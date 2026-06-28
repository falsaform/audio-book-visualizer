"""Run a crew role against an LLM and return structured JSON.

Two backends, both yielding the same structured output:
- **staged** (default): one structured ``complete_json`` call per role — works
  with an API key or the Claude Code token.
- **autonomous**: the role runs as a multi-turn Claude Code agent
  (``ClaudeCodeClient.run_agent``) — needs the ``claude`` CLI.

Clients are built per role *model* (so the roster can mix models) and cached.
"""

from __future__ import annotations

from typing import Any

from ..config import AnalysisConfig, RoleConfig
from ..utils.llm import ClaudeCodeClient, _parse_json, build_llm_client


class AgentRunner:
    def __init__(self, analysis_cfg: AnalysisConfig, agent_mode: bool = False) -> None:
        self._cfg = analysis_cfg
        self._agent_mode = agent_mode
        self._clients: dict[str, Any] = {}

    def _client(self, model: str):
        if model not in self._clients:
            self._clients[model] = build_llm_client(
                self._cfg.model_copy(update={"model": model})
            )
        return self._clients[model]

    def json(self, role: RoleConfig, system: str, prompt: str, max_tokens: int = 4096) -> Any:
        client = self._client(role.model)
        sys_prompt = role.system_prompt or system
        if self._agent_mode and isinstance(client, ClaudeCodeClient):
            return _parse_json(client.run_agent(sys_prompt, prompt, max_turns=role.max_turns))
        return client.complete_json(sys_prompt, prompt, max_tokens=max_tokens)
