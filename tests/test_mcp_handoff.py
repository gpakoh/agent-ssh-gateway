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

        assert tm.should_register_tool("write_handoff_plan") is True
        assert tm.should_register_tool("read_handoff") is True
        assert tm.should_register_tool("show_handoff_status") is True

    def test_standard_excludes_handoff_tools(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "standard")
        tm = _import(monkeypatch, "tool_modes")

        assert tm.should_register_tool("write_handoff_plan") is False


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


class TestProjectHandoffStatus:
    """The status read must not turn unavailable metadata into absent work."""

    @staticmethod
    def _tools(monkeypatch, root):
        # Preserve module identity: other collected tests import functions
        # from this module and patch its globals by module name.
        monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
        tools = importlib.import_module("mcp_client_tools")
        monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
        # The old ls path would fail with permission denied, then claim absence.
        monkeypatch.setattr(tools, "run_project_command", lambda *_args: {
            "outcome": "failed", "exit_code": 2, "stderr": "Permission denied",
        })
        return tools

    def test_missing_directory_is_distinct_from_missing_root(self, tmp_path, monkeypatch):
        tools = self._tools(monkeypatch, tmp_path)
        result = tools.show_handoff_status(None, "example")
        assert result["exit_code"] == 0
        assert result["stdout"] == "(no .ai-bridge directory)"
        assert result["handoff_status"]["state"] == "absent"
        assert result["handoff_status"]["authorizes_mutation"] is False

        monkeypatch.setattr(tools, "_resolve_project", lambda _project: tmp_path / "gone")
        result = tools.show_handoff_status(None, "example")
        assert result["exit_code"] != 0
        assert result["handoff_status"]["state"] == "unavailable"
        assert "no .ai-bridge" not in result["stdout"]

    def test_fixed_metadata_only_and_freshness_unbound(self, tmp_path, monkeypatch):
        tools = self._tools(monkeypatch, tmp_path)
        bridge = tmp_path / ".ai-bridge"
        bridge.mkdir()
        (bridge / "current-plan.md").write_text("PRIVATE_PLAN_BODY")
        (bridge / "other-private-file").write_text("PRIVATE_UNRELATED")
        result = tools.show_handoff_status(None, "example")
        status = result["handoff_status"]
        assert result["exit_code"] == 0
        assert status["state"] == "observed"
        assert status["observation_plane"] == "registered_host_workspace"
        assert status["freshness"] == "unbound"
        assert status["atomic_snapshot"] is False
        assert status["files"]["current_plan"]["state"] == "present"
        assert status["files"]["current_plan"]["size_bytes"] == len("PRIVATE_PLAN_BODY")
        assert status["files"]["agent_status"]["state"] == "absent"
        assert status["files"]["implementation_diff"]["state"] == "absent"
        assert "PRIVATE" not in str(result)
        assert "other-private-file" not in str(result)
        assert str(tmp_path) not in str(result)

    def test_symlink_directory_and_file_are_not_followed(self, tmp_path, monkeypatch):
        tools = self._tools(monkeypatch, tmp_path)
        external = tmp_path / "external"
        external.mkdir()
        (external / "current-plan.md").write_text("PRIVATE_PLAN_BODY")
        bridge = tmp_path / ".ai-bridge"
        bridge.symlink_to(external, target_is_directory=True)
        result = tools.show_handoff_status(None, "example")
        assert result["exit_code"] != 0
        assert result["handoff_status"]["state"] == "unavailable"
        bridge.unlink()
        bridge.mkdir()
        (bridge / "current-plan.md").symlink_to(external / "current-plan.md")
        result = tools.show_handoff_status(None, "example")
        status = result["handoff_status"]
        assert status["state"] == "partial"
        assert status["files"]["current_plan"] == {"state": "unsafe"}
        assert status["freshness"] == "unknown"
        assert "PRIVATE_PLAN_BODY" not in str(result)

    @pytest.mark.parametrize("failure", [PermissionError, TimeoutError])
    def test_failed_directory_probe_is_not_absence(self, tmp_path, monkeypatch, failure):
        tools = self._tools(monkeypatch, tmp_path)
        original_open = tools.os.open

        def fail_bridge(path, *args, **kwargs):
            if path == ".ai-bridge":
                raise failure("PRIVATE_FAILURE_DETAILS")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(tools.os, "open", fail_bridge)
        result = tools.show_handoff_status(None, "example")
        assert result["exit_code"] != 0
        assert result["handoff_status"]["state"] == "unavailable"
        assert "PRIVATE_FAILURE_DETAILS" not in str(result)
        assert "no .ai-bridge" not in result["stdout"]
