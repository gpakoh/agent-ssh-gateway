"""Tests for OpenCode runner MCP tool — opencode_tools module.

project_run_opencode runs real --dangerously-skip-permissions execution --
the earlier "C3" hard block was deliberately lifted so run_opencode can
actually launch opencode, including async_submit=True for fleet mode.
Still gated by write-mode (assert_handoff_write_allowed, checked by
gateway_run_opencode) and by tool-mode registration (excluded from
mcp_client/mcp_client_write's tool sets, see tool_modes.py) -- neither is
exercised by this file.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import anyio
import pytest

MCP_DIR = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)

from examples.mcp_server.opencode_tools import project_run_opencode  # noqa: E402

TASK_ID = "2026-06-25-fix-auth-opencode"


def _fake_run_cmd(current_plan: str = "# Plan\n\n1. Do the thing", task_json: dict | None = None) -> MagicMock:
    if task_json is None:
        task_json = {"worktree_path": "../agent-worktrees/test-opencode"}

    def fn(project: str, command: str) -> dict:
        if command.startswith("cat ") and "task.json" in command:
            return {
                "exit_code": 0,
                "stdout": json.dumps(task_json),
                "stderr": "",
            }
        if command.startswith("cat ") and "current-plan.md" in command:
            return {"exit_code": 0, "stdout": current_plan, "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    return MagicMock(side_effect=fn)


class TestProjectRunOpencodeExecutes:
    """project_run_opencode runs for real -- no confirmation flow, no
    override, but also no block: write-mode + tool-mode registration are
    the only gates, both enforced above this function."""

    def test_runs_and_returns_result(self):
        rc = _fake_run_cmd()
        run_script = MagicMock(return_value={"exit_code": 0, "stdout": "ok", "stderr": ""})
        result = project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert result["status"] == "needs-review"
        assert result["exit_code"] == 0
        assert result["stdout"] == "ok"
        run_script.assert_called_once()

    def test_falls_back_to_run_cmd_when_no_run_script(self):
        rc = _fake_run_cmd()
        result = project_run_opencode(rc, project="test", task_id=TASK_ID)
        # rc's catch-all branch returns exit_code=0, empty stdout/stderr
        assert result["status"] == "needs-review"

    def test_nonzero_exit_reports_failed(self):
        rc = _fake_run_cmd()
        run_script = MagicMock(return_value={"exit_code": 1, "stdout": "", "stderr": "boom"})
        result = project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert result["status"] == "failed"
        assert result["exit_code"] == 1

    def test_proxy_block_exit_reports_blocked(self):
        rc = _fake_run_cmd()
        run_script = MagicMock(return_value={"exit_code": 76, "stdout": "", "stderr": ""})
        result = project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert result["status"] == "blocked"
        assert result["exit_code"] == 76

    def test_sigkill_exit_reports_resource_exhausted(self):
        rc = _fake_run_cmd()
        run_script = MagicMock(return_value={"exit_code": 137, "stdout": "Killed", "stderr": ""})
        result = project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert result["status"] == "resource-exhausted"
        assert result["exit_code"] == 137

    def test_no_current_plan_errors_before_execution(self):
        rc = _fake_run_cmd(current_plan="")
        run_script = MagicMock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})
        result = project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert result["status"] == "error"
        assert "current-plan.md" in result["error"]
        run_script.assert_not_called()

    def test_script_cds_into_project_root(self, monkeypatch):
        """Regression: confirmed live via a real MCP run_opencode-equivalent
        (run_agent) call -- the async dispatch path has no cwd concept, so
        the script's relative $td references silently resolved against the
        SSH session's home dir instead of the project root. The script
        must cd into the absolute project root itself."""
        monkeypatch.setattr(
            "app.workspace.registry.get_registry",
            lambda: type("R", (), {"project_info": lambda self, p: {"root": "/abs/project/root"}})(),
        )
        rc = _fake_run_cmd()
        captured: dict[str, str] = {}

        def run_script(project, script):
            captured["script"] = script
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert captured["script"].startswith("cd '/abs/project/root' || exit 1")

    def test_model_override_reaches_the_script(self):
        rc = _fake_run_cmd()
        captured: dict[str, str] = {}

        def run_script(project, script):
            captured["script"] = script
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        project_run_opencode(rc, project="test", task_id=TASK_ID, model="gpt-4o", run_script=run_script)
        assert "--model 'gpt-4o'" in captured["script"]

    def test_task_base_ref_reaches_generated_script(self):
        base_ref = "a" * 40
        rc = _fake_run_cmd(
            task_json={"worktree_path": "../agent-worktrees/test-opencode", "base_ref": base_ref}
        )
        captured: dict[str, str] = {}

        def run_script(project, script):
            captured["script"] = script
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        result = project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert result["status"] == "needs-review"
        assert f"TASK_BASE_REF='{base_ref}'" in captured["script"]

    def test_invalid_task_base_ref_errors_before_execution(self):
        rc = _fake_run_cmd(
            task_json={"worktree_path": "../agent-worktrees/test-opencode", "base_ref": "master"}
        )
        run_script = MagicMock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})
        result = project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert result["status"] == "error"
        assert "Invalid base_ref" in result["error"]
        run_script.assert_not_called()
    def test_non_string_task_base_ref_errors_before_execution(self):
        rc = _fake_run_cmd(
            task_json={"worktree_path": "../agent-worktrees/test-opencode", "base_ref": 123}
        )
        run_script = MagicMock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})
        result = project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert result["status"] == "error"
        assert "base_ref must be a string or None" in result["error"]
        run_script.assert_not_called()


    def test_managed_mode_uses_immutable_source_bundle(self, monkeypatch):
        base_ref = "c" * 40
        monkeypatch.setenv("MCP_AGENT_WORKSPACE_ROOT", "/var/lib/mcp-agent/workspaces")
        monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", "/var/lib/mcp-agent/sources")
        monkeypatch.setattr(
            "app.workspace.registry.get_registry",
            lambda: type("R", (), {"project_info": lambda self, p: {"root": "/abs/project/root"}})(),
        )
        rc = _fake_run_cmd(
            task_json={"worktree_path": "/attacker/path", "base_ref": base_ref}
        )
        captured: dict[str, str] = {}

        def run_script(project, script):
            captured["script"] = script
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        result = project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert result["status"] == "needs-review"
        script = captured["script"]
        assert "/attacker/path" not in script
        assert f"/{base_ref}.bundle" in script
        assert 'git clone --no-hardlinks --no-checkout "$MANAGED_SOURCE_BUNDLE" "$wt"' in script
        assert 'git clone --no-hardlinks --no-checkout "$PARENT_ROOT" "$wt"' not in script

    def test_managed_mode_requires_pinned_base_ref(self, monkeypatch):
        monkeypatch.setenv("MCP_AGENT_WORKSPACE_ROOT", "/var/lib/mcp-agent/workspaces")
        monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", "/var/lib/mcp-agent/sources")
        monkeypatch.setattr(
            "app.workspace.registry.get_registry",
            lambda: type("R", (), {"project_info": lambda self, p: {"root": "/abs/project/root"}})(),
        )
        rc = _fake_run_cmd(task_json={"worktree_path": "/attacker/path"})
        run_script = MagicMock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})
        result = project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert result["status"] == "error"
        assert "exact base_ref" in result["error"]
        run_script.assert_not_called()

    def test_dangerously_skip_permissions_present_in_generated_script(self):
        """This is the whole point of the tool -- opencode's own safety
        confirmations must be disabled for unattended execution."""
        rc = _fake_run_cmd()
        captured: dict[str, str] = {}

        def run_script(project, script):
            captured["script"] = script
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        project_run_opencode(rc, project="test", task_id=TASK_ID, run_script=run_script)
        assert "--dangerously-skip-permissions" in captured["script"]


class TestProjectRunOpencodeAsyncSubmit:
    def test_returns_job_id_immediately(self):
        rc = _fake_run_cmd()
        run_script_async = MagicMock(return_value={"job_id": "job-77"})
        result = project_run_opencode(
            rc, project="test", task_id=TASK_ID, async_submit=True, run_script_async=run_script_async
        )
        assert result["status"] == "running"
        assert result["job_id"] == "job-77"
        assert result["exit_code"] is None
        run_script_async.assert_called_once()

    def test_missing_run_script_async_errors(self):
        rc = _fake_run_cmd()
        result = project_run_opencode(rc, project="test", task_id=TASK_ID, async_submit=True)
        assert result["status"] == "error"
        assert "async_submit" in result["error"]

    def test_no_current_plan_skips_submit(self):
        rc = _fake_run_cmd(current_plan="")
        run_script_async = MagicMock(return_value={"job_id": "job-1"})
        result = project_run_opencode(
            rc, project="test", task_id=TASK_ID, async_submit=True, run_script_async=run_script_async
        )
        assert result["status"] == "error"
        run_script_async.assert_not_called()


class TestServerWrapperWired:
    """Server.py's gateway_run_opencode wraps project_run_opencode with the
    real client -- verify the wiring (write-mode gate passes, client
    methods get called with the right shape) end to end through a fresh
    server.py reimport, without a live SSH session.
    """

    @pytest.mark.skipif(
        not importlib.util.find_spec("mcp"),
        reason="mcp package not installed",
    )
    def test_server_wrapper_executes_via_client(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "handoff")
        monkeypatch.setenv("GITEA_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        import importlib
        import sys
        from pathlib import Path

        example_dir = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
        monkeypatch.syspath_prepend(str(example_dir))
        # Clear ALL modules that server.py imports so a clean reimport occurs
        clear_prefixes = ("server", "mcp_server", "tool_modes", "opencode_tools", "command_policy",
                          "gateway_client", "handoff", "self_test", "write_modes",
                          "docker_confirm", "agent_tools", "agent_tasks",
                          "agent_backend_router", "mcp_client_tools")
        saved_modules = {}
        for name in list(sys.modules):
            if any(p in name for p in clear_prefixes):
                saved_modules[name] = sys.modules.pop(name)
        try:
            server = importlib.import_module("server")
            tool_fn = getattr(server, "gateway_run_opencode", None)
            assert tool_fn is not None

            server.client.execute_project_command = MagicMock(
                return_value={"exit_code": 0, "stdout": "# Plan", "stderr": ""}
            )
            server.client.execute_project_script = MagicMock(
                return_value={"exit_code": 0, "stdout": "done", "stderr": ""}
            )

            result = anyio.run(lambda: tool_fn(project="test", task_id=TASK_ID))
            assert result.get("ok") is True
            server.client.execute_project_script.assert_called_once()
        finally:
            for name in [
                n
                for n in list(sys.modules)
                if any(p in n for p in clear_prefixes) and n not in saved_modules
            ]:
                del sys.modules[name]
            sys.modules.update(saved_modules)
            # update() restores sys.modules but NOT parent-package attributes:
            # reimporting "server" re-created the examples.mcp_server namespace
            # package and rebound examples.mcp_server to the new object. Restore
            # the original parent attributes so later imports (e.g. monkeypatch
            # on examples.mcp_server.mcp_client_tools._resolve_project) resolve
            # to the pre-test modules instead of hitting stale objects.
            for name, module in saved_modules.items():
                if "." in name:
                    parent_name, _, attr = name.rpartition(".")
                    parent = sys.modules.get(parent_name)
                    if parent is not None and getattr(parent, attr, None) is not module:
                        setattr(parent, attr, module)
