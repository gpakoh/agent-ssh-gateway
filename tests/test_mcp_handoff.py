"""Tests for MCP handoff mode and write permission."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"


def _import(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    sys.modules.pop(name, None)
    return importlib.import_module(name)


class TestWriteModes:
    def test_defaults_to_off(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MCP_GATEWAY_WRITE_MODE", raising=False)
        wm = _import(monkeypatch, "write_modes")
        assert wm.get_write_mode() == "off"

    def test_handoff_write_blocked_in_off(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "off")
        wm = _import(monkeypatch, "write_modes")
        with pytest.raises(wm.WritePermissionError):
            wm.assert_handoff_write_allowed()

    def test_handoff_write_allowed_in_handoff(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "handoff")
        wm = _import(monkeypatch, "write_modes")
        wm.assert_handoff_write_allowed()

    def test_handoff_write_allowed_in_full(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "full")
        wm = _import(monkeypatch, "write_modes")
        wm.assert_handoff_write_allowed()

    def test_invalid_mode_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "bogus")
        wm = _import(monkeypatch, "write_modes")
        with pytest.raises(wm.WriteModeError):
            wm.get_write_mode()


class TestBuildHandoffPlan:
    def test_contains_task(self, monkeypatch: pytest.MonkeyPatch):
        handoff = _import(monkeypatch, "handoff")
        plan = handoff.build_handoff_plan("Fix failing tests", agent="opencode")
        assert "Fix failing tests" in plan

    def test_contains_contract(self, monkeypatch: pytest.MonkeyPatch):
        handoff = _import(monkeypatch, "handoff")
        plan = handoff.build_handoff_plan("fix", agent="opencode")
        assert ".ai-bridge/agent-status.md" in plan
        assert ".ai-bridge/implementation-diff.patch" in plan
        assert "Do not expose secrets" in plan

    def test_contains_notes_when_provided(self, monkeypatch: pytest.MonkeyPatch):
        handoff = _import(monkeypatch, "handoff")
        plan = handoff.build_handoff_plan("fix", notes="Use Python 3.12")
        assert "Use Python 3.12" in plan

    def test_no_notes_when_omitted(self, monkeypatch: pytest.MonkeyPatch):
        handoff = _import(monkeypatch, "handoff")
        plan = handoff.build_handoff_plan("fix")
        assert "## Additional notes" not in plan


class TestWriteHandoffPlan:
    def test_writes_only_current_plan(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "handoff")
        handoff = _import(monkeypatch, "handoff")

        class FakeClient:
            def __init__(self):
                self.calls = []

            def write_file(self, path, content, session_id=None, mode="overwrite"):
                self.calls.append((path, content, session_id, mode))
                return {"path": path, "mode": mode}

        client = FakeClient()
        result = handoff.write_handoff_plan(client, task="Do work", session_id="s1")
        assert result["path"] == ".ai-bridge/current-plan.md"
        assert client.calls[0][0] == ".ai-bridge/current-plan.md"
        assert client.calls[0][2] == "s1"
        assert client.calls[0][3] == "overwrite"

    def test_blocked_when_write_mode_off(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "off")
        handoff = _import(monkeypatch, "handoff")

        with pytest.raises(PermissionError):
            handoff.write_handoff_plan(None, task="x")  # type: ignore[arg-type]


class TestHandoffToolVisibility:
    def test_full_includes_handoff_tools(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "full")
        tm = _import(monkeypatch, "tool_modes")

        assert tm.should_register_tool("gateway_write_handoff_plan") is True
        assert tm.should_register_tool("gateway_read_handoff") is True
        assert tm.should_register_tool("gateway_show_handoff_status") is True

    def test_standard_excludes_handoff_tools(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "standard")
        tm = _import(monkeypatch, "tool_modes")

        assert tm.should_register_tool("gateway_write_handoff_plan") is False


class TestReadHandoff:
    def test_returns_paths(self, monkeypatch: pytest.MonkeyPatch):
        handoff = _import(monkeypatch, "handoff")

        class FakeClient:
            api_key = "k"
            session_id = "s"

            def read_file(self, path, session_id=None):
                return {"content": "# plan", "path": path}

        result = handoff.read_handoff(FakeClient(), session_id="s")  # type: ignore[arg-type]
        assert "current_plan" in result["files"]
        assert result["paths"]["current_plan"] == ".ai-bridge/current-plan.md"
        assert result["paths"]["agent_status"] == ".ai-bridge/agent-status.md"

    def test_errors_on_read_failure(self, monkeypatch: pytest.MonkeyPatch):
        handoff = _import(monkeypatch, "handoff")

        class FailingClient:
            api_key = "k"
            session_id = "s"

            def read_file(self, path, session_id=None):
                raise RuntimeError("not found")

        result = handoff.read_handoff(FailingClient(), session_id="s")  # type: ignore[arg-type]
        assert "current_plan" in result["errors"]
        assert result["files"] == {}
