"""Tests for analysis client selection and the Claude Code CLI backend."""

import json
import types

import pytest

import audiobook_visualizer.utils.llm as llm
from audiobook_visualizer.config import AnalysisConfig


def test_build_client_auto_prefers_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-test")
    # Avoid constructing the real SDK client.
    monkeypatch.setattr(llm, "ClaudeClient", lambda model, temp: ("sdk", model))
    client = llm.build_llm_client(AnalysisConfig(provider="auto"))
    assert client[0] == "sdk"


def test_build_client_auto_falls_back_to_oauth(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-test")
    monkeypatch.setattr(llm, "ClaudeCodeClient", lambda model, temp: ("cli", model))
    client = llm.build_llm_client(AnalysisConfig(provider="auto"))
    assert client[0] == "cli"


def test_build_client_errors_without_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="No analysis credentials"):
        llm.build_llm_client(AnalysisConfig(provider="auto"))


def test_claude_code_client_parses_cli_json(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-test")
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/bin/claude")

    captured = {}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        captured["cmd"] = cmd
        captured["input"] = input
        payload = {"type": "result", "is_error": False, "result": '[{"name": "Ahab"}]'}
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)

    client = llm.ClaudeCodeClient(model="claude-sonnet-4-6")
    result = client.complete_json("SYSTEM PROMPT", "analyze this")

    assert result == [{"name": "Ahab"}]
    # Prompt is piped via stdin; system prompt and model are passed as flags.
    assert captured["input"] == "analyze this"
    assert "--append-system-prompt" in captured["cmd"]
    assert "SYSTEM PROMPT" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "claude-sonnet-4-6"
    assert "--max-turns" in captured["cmd"]


def test_claude_code_client_raises_on_cli_error(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-test")
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("time.sleep", lambda _s: None)  # skip retry backoff

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="auth failed")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    client = llm.ClaudeCodeClient(model="sonnet")
    with pytest.raises(RuntimeError, match="auth failed"):
        client.complete("sys", "prompt")


def test_claude_code_client_requires_cli(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="claude' CLI was not found"):
        llm.ClaudeCodeClient(model="sonnet")
