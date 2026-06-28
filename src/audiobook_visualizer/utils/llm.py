"""Thin wrapper over the Anthropic API for structured (JSON) analysis calls.

Analysis stages ask Claude for JSON. The model is reliable at this when given a
clear schema, but we still defensively extract the first JSON value from the
response in case it wraps output in prose or code fences.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import require_env

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ClaudeClient:
    """Small convenience wrapper around ``anthropic.Anthropic``."""

    def __init__(self, model: str, temperature: float = 0.4) -> None:
        # Imported lazily so the package imports without the SDK installed.
        from anthropic import Anthropic

        self.model = model
        self.temperature = temperature
        self._client = Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, max=30))
    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=self.temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")

    def complete_json(
        self, system: str, prompt: str, max_tokens: int = 4096
    ) -> Any:
        """Call the model and parse its response as JSON."""
        text = self.complete(system, prompt, max_tokens=max_tokens)
        return _parse_json(text)


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
