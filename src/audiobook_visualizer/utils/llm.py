"""LLM clients for the analysis stage.

Two interchangeable backends, both exposing ``complete`` / ``complete_json``:

* :class:`ClaudeClient` — the Anthropic Python SDK, authenticated with
  ``ANTHROPIC_API_KEY``.
* :class:`ClaudeCodeClient` — shells out to the bundled ``claude`` CLI, which
  authenticates with a Claude Code subscription token (``CLAUDE_CODE_OAUTH_TOKEN``).
  Use this when you have a Claude Code token but no raw API key.

:func:`build_llm_client` picks one based on config and the available credentials.

Analysis asks Claude for JSON. The model is reliable at this given a clear
schema, but we still defensively extract the first JSON value from the response
in case it wraps output in prose or code fences.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import AnalysisConfig, require_env

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class _BaseClient:
    """Shared ``complete_json`` behaviour over a backend-specific ``complete``."""

    model: str

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        raise NotImplementedError

    def complete_json(self, system: str, prompt: str, max_tokens: int = 4096) -> Any:
        return _parse_json(self.complete(system, prompt, max_tokens=max_tokens))


class ClaudeClient(_BaseClient):
    """Anthropic SDK client, authenticated with ``ANTHROPIC_API_KEY``."""

    def __init__(self, model: str, temperature: float = 0.4) -> None:
        # Imported lazily so the package imports without the SDK installed.
        from anthropic import Anthropic

        self.model = model
        self.temperature = temperature
        self._client = Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=self.temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")


class ClaudeCodeClient(_BaseClient):
    """Drives the ``claude`` CLI, authenticated with ``CLAUDE_CODE_OAUTH_TOKEN``.

    The CLI handles OAuth, so no raw API key is needed. We run it non-interactively
    (``-p``) with ``--max-turns 1`` (one response, no agentic tool loops) and read
    the JSON envelope's ``result`` field. ``temperature``/``max_tokens`` aren't
    exposed by the CLI and are ignored.
    """

    def __init__(self, model: str, temperature: float = 0.4) -> None:
        self.model = model
        self.temperature = temperature  # accepted for API parity; unused by the CLI
        self._bin = shutil.which("claude")
        if not self._bin:
            raise RuntimeError(
                "The 'claude' CLI was not found on PATH. It ships in the Docker "
                "image; run via `just`/docker, or install Claude Code locally."
            )
        require_env("CLAUDE_CODE_OAUTH_TOKEN")

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        cmd = [
            self._bin, "-p",
            "--output-format", "json",
            "--model", self.model,
            "--max-turns", "1",
        ]
        if system:
            cmd += ["--append-system-prompt", system]
        # Prompt goes over stdin to avoid command-line length limits.
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=600
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            )
        return _extract_cli_result(proc.stdout)

    def run_agent(
        self,
        system: str,
        prompt: str,
        max_turns: int = 6,
        allowed_tools: str = "Read",
        cwd: Optional[str] = None,
    ) -> str:
        """Run the CLI as an autonomous, multi-turn agent and return its final text.

        Unlike :meth:`complete` (one turn, no tools), this lets the model reason
        across up to ``max_turns`` turns and use the read-only tools in
        ``allowed_tools`` (e.g. to read context files dropped in ``cwd``). The
        caller still parses the final answer as JSON.
        """
        cmd = [
            self._bin, "-p",
            "--output-format", "json",
            "--model", self.model,
            "--max-turns", str(max(1, max_turns)),
        ]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        if system:
            cmd += ["--append-system-prompt", system]
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=1800, cwd=cwd
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude agent failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            )
        return _extract_cli_result(proc.stdout)


def _usable_key(value: Optional[str]) -> bool:
    """True for a real credential, False for empty or placeholder values.

    Guards the common footgun of a leftover ``sk-ant-...`` placeholder in .env
    hijacking ``auto`` selection (and then 401-ing on every request).
    """
    if not value:
        return False
    v = value.strip()
    return bool(v) and "..." not in v


def build_llm_client(config: AnalysisConfig) -> _BaseClient:
    """Select an analysis client from config and the available credentials.

    ``provider``: ``auto`` (default) prefers a real API key, then a Claude Code
    token; ``anthropic`` forces the SDK; ``claude-code`` forces the CLI.
    """
    provider = (config.provider or "auto").lower()
    if provider == "anthropic":
        return ClaudeClient(config.model, config.temperature)
    if provider in ("claude-code", "claude_code", "cli"):
        return ClaudeCodeClient(config.model, config.temperature)

    # auto: a real API key wins; otherwise fall back to the Claude Code token.
    if _usable_key(os.environ.get("ANTHROPIC_API_KEY")):
        return ClaudeClient(config.model, config.temperature)
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return ClaudeCodeClient(config.model, config.temperature)
    raise RuntimeError(
        "No analysis credentials found. Set ANTHROPIC_API_KEY, or "
        "CLAUDE_CODE_OAUTH_TOKEN to use the bundled Claude Code CLI."
    )


def _extract_cli_result(stdout: str) -> str:
    """Pull the assistant text from `claude -p --output-format json` output."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout  # already plain text
    if isinstance(data, dict):
        if data.get("is_error"):
            raise RuntimeError(f"claude CLI reported an error: {data.get('result')}")
        return str(data.get("result", stdout))
    return stdout


def _parse_json(text: str) -> Any:
    """Best-effort extraction of a JSON value from model output."""
    text = text.strip()
    # 1. Try a fenced code block first.
    fence = _FENCE_RE.search(text)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 2. Try the whole string.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 3. Try to slice from the first bracket to the last matching one.
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Could not parse JSON from model response:\n{text[:500]}")
