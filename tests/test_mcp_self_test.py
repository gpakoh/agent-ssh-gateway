"""Tests for MCP gateway self-test."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"


def _import(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    sys.modules.pop(name, None)
    return importlib.import_module(name)


class FakeClient:
    api_key = "test-key"
    session_id = "test-session"

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    def session_health(self, session_id: str | None = None) -> dict[str, Any]:
        return {"connected": True}

    def repo_status(self, session_id: str | None = None) -> dict[str, Any]:
        return {"status": {"exit_code": 0}}


class FakeClientNoSession:
    api_key = "test-key"
    session_id = ""

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}


class TestSelfTest:
    def test_passes_with_fake_client(self, monkeypatch: pytest.MonkeyPatch):
        self_test = _import(monkeypatch, "self_test")
        result = self_test.run_self_test(FakeClient())
        assert result["status"] == "pass"
        assert result["summary"]["fail"] == 0

    def test_warns_without_session(self, monkeypatch: pytest.MonkeyPatch):
        self_test = _import(monkeypatch, "self_test")
        result = self_test.run_self_test(FakeClientNoSession())
        assert result["status"] == "warn"
        assert result["summary"]["warn"] >= 1

    def test_checks_are_list(self, monkeypatch: pytest.MonkeyPatch):
        self_test = _import(monkeypatch, "self_test")
        result = self_test.run_self_test(FakeClient())
        assert isinstance(result["checks"], list)
        assert len(result["checks"]) >= 8

    def test_summary_counts_match(self, monkeypatch: pytest.MonkeyPatch):
        self_test = _import(monkeypatch, "self_test")
        result = self_test.run_self_test(FakeClient())
        total = result["summary"]["pass"] + result["summary"]["warn"] + result["summary"]["fail"]
        assert total == len(result["checks"])

    def test_full_mode_includes_self_test(self, monkeypatch: pytest.MonkeyPatch):
        tool_modes = _import(monkeypatch, "tool_modes")
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "full")
        assert tool_modes.should_register_tool("self_test") is True

    def test_standard_mode_excludes_self_test(self, monkeypatch: pytest.MonkeyPatch):
        tool_modes = _import(monkeypatch, "tool_modes")
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "standard")
        assert tool_modes.should_register_tool("self_test") is False

    def test_check_result_shape(self, monkeypatch: pytest.MonkeyPatch):
        self_test = _import(monkeypatch, "self_test")
        c = self_test.check_result("test", "pass", "ok")
        assert c == {"name": "test", "status": "pass", "detail": "ok", "data": {}}
