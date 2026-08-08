"""Tests for ChatGPT-safe MCP tool profile."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from examples.mcp_server.tool_modes import TOOL_NAMES_BY_MODE, should_register_tool

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"


def import_example_module(monkeypatch: pytest.MonkeyPatch, module_name: str):
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    # Keep the pre-existing module object around so monkeypatch can restore
    # it at teardown; a bare pop() would leave the reloaded copy in
    # sys.modules and break import identity for later tests.
    old = sys.modules.get(module_name)
    monkeypatch.setitem(sys.modules, module_name, old)
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


class TestChatgptModeVisibility:
    def test_excludes_generic_execute(self):
        assert not should_register_tool("execute_restricted", "mcp_client")

    def test_includes_health(self):
        assert should_register_tool("health", "mcp_client")

    def test_includes_session_health(self):
        assert should_register_tool("session_health", "mcp_client")

    def test_includes_git_status(self):
        assert should_register_tool("git_status", "mcp_client")

    def test_includes_recent_commits(self):
        assert should_register_tool("recent_commits", "mcp_client")

    def test_includes_git_diff_stat(self):
        assert should_register_tool("git_diff_stat", "mcp_client")

    def test_includes_show_changes(self):
        assert should_register_tool("show_changes", "mcp_client")

    def test_includes_run_tests(self):
        assert should_register_tool("run_tests", "mcp_client")

    def test_includes_run_lint(self):
        assert should_register_tool("run_lint", "mcp_client")

    def test_includes_run_compileall(self):
        assert should_register_tool("run_compileall", "mcp_client")

    def test_includes_working_directory(self):
        assert should_register_tool("working_directory", "mcp_client")

    def test_includes_handoff_tools(self):
        assert should_register_tool("read_handoff", "mcp_client")
        assert should_register_tool("write_handoff_plan", "mcp_client")
        assert should_register_tool("show_handoff_status", "mcp_client")

    def test_includes_jobs(self):
        assert should_register_tool("job_status", "mcp_client")
        assert should_register_tool("job_result", "mcp_client")
        assert should_register_tool("wait_job", "mcp_client")

    def test_includes_read_file(self):
        assert should_register_tool("read_file", "mcp_client")

    def test_includes_repo_status(self):
        assert should_register_tool("repo_status", "mcp_client")

    def test_excludes_list_sessions(self):
        assert not should_register_tool("list_sessions", "mcp_client")

    def test_excludes_self_test(self):
        assert not should_register_tool("self_test", "mcp_client")

    def test_mcp_client_is_known_mode(self):
        assert "mcp_client" in TOOL_NAMES_BY_MODE


class _FakeClient:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def execute_restricted(self, command: str, session_id: str | None = None) -> dict:
        self.commands.append(command)
        return {"job_id": f"job-{len(self.commands)}"}

    def wait_job(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
        }


class TestWorkingDirectoryNoHostPaths:
    """T2.3: working_directory must not return absolute host paths."""

    @pytest.fixture(autouse=True)
    def _registry(self, tmp_path):
        """The tool resolves the project through the workspace registry; on
        CI the real /media/1TB/Python root does not exist, so load a local
        registry containing web-ssh-gateway (audit T31 #13 regression)."""
        import app.workspace.registry as registry_module
        from app.workspace.registry import WorkspaceRegistry, reset_registry

        project_root = tmp_path / "web-ssh-gateway"
        project_root.mkdir()
        yaml_path = tmp_path / "projects.yaml"
        yaml_path.write_text(
            f"""
registry_root: {tmp_path}
projects:
  web-ssh-gateway:
    root: web-ssh-gateway
    type: fastapi
    description: test project
    tags: []
"""
        )
        reset_registry()
        registry_module._registry = WorkspaceRegistry.load(yaml_path)
        yield
        reset_registry()

    def test_returns_project_relative_dot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")
        result = mod.working_directory(_FakeClient(), "web-ssh-gateway")
        assert result["stdout"] == "."
        assert result["exit_code"] == 0
        assert result["outcome"] == "passed"

    def test_never_shells_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")
        called: list[str] = []

        class _Probe:
            def execute_project_command(self, project: str, command: str) -> dict:
                called.append(command)
                return {"exit_code": 0, "stdout": "/media/1TB/Python/web-ssh-gateway\n"}

        result = mod.working_directory(_Probe(), "web-ssh-gateway")
        assert result["stdout"] == "."
        assert called == []


class TestReadFileErrorCodes:
    """T25: read_file must distinguish a missing file (FILE_NOT_FOUND)
    from a policy denial (POLICY_DENIED) and preserve the secret-path
    and read-error codes instead of degrading them to INTERNAL_ERROR."""

    def test_read_file_missing_returns_file_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")

        def _raise_missing(*args, **kwargs):
            from app.workspace.policy import FileNotFoundInWorkspaceError

            raise FileNotFoundInWorkspaceError("File not found: nope.txt")

        monkeypatch.setattr(
            "app.workspace.files.project_file_read", _raise_missing
        )
        result = mod.read_file(_FakeClient(), "web-ssh-gateway", "nope.txt")
        assert result["error"]["code"] == "FILE_NOT_FOUND"

    def test_read_file_hidden_path_preserves_secret_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")

        def _raise_hidden(*args, **kwargs):
            from app.workspace.policy import HiddenPathError

            raise HiddenPathError("Denied: .env.local")

        monkeypatch.setattr(
            "app.workspace.files.project_file_read", _raise_hidden
        )
        result = mod.read_file(_FakeClient(), "web-ssh-gateway", ".env.local")
        assert result["error"]["code"] == "SECRET_PATH_DENIED"

    def test_read_file_generic_failure_preserves_read_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")

        def _raise_boom(*args, **kwargs):
            raise OSError("disk exploded")

        monkeypatch.setattr(
            "app.workspace.files.project_file_read", _raise_boom
        )
        result = mod.read_file(_FakeClient(), "web-ssh-gateway", "main.py")
        assert result["error"]["code"] == "FILE_READ_ERROR"


class TestShowChangesErrorEnvelope:
    """show_changes must surface git diagnostics when both calls fail."""

    def test_both_fail_returns_error_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")

        failed = {"exit_code": 128, "stdout": "fatal: not a git repository", "stderr": ""}

        monkeypatch.setattr(mod, "git_status", lambda client, project: failed)
        monkeypatch.setattr(mod, "git_diff_stat", lambda client, project: failed)

        result = mod.show_changes(_FakeClient(), "demo")
        assert result["ok"] is False
        assert result["error"]["code"] == "CHECK_FAILED"
        assert result["error"]["details"]["git_status"]["stdout"] == (
            "fatal: not a git repository"
        )

    def test_single_failure_still_reports_ok_with_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")

        ok = {"exit_code": 0, "stdout": " M main.py", "stderr": ""}
        failed = {"exit_code": 128, "stdout": "fatal: not a git repository", "stderr": ""}

        monkeypatch.setattr(mod, "git_status", lambda client, project: ok)
        monkeypatch.setattr(mod, "git_diff_stat", lambda client, project: failed)

        result = mod.show_changes(_FakeClient(), "demo")
        assert "ok" not in result
        assert result["git_status"]["exit_code"] == 0


class TestReadHandoffErrorDistinction:
    """read_handoff must not mask real errors as '(no handoff plan)'."""

    def test_missing_plan_reports_no_handoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")

        missing = {
            "exit_code": 1,
            "stdout": "",
            "stderr": "cat: .ai-bridge/current-plan.md: No such file or directory",
        }
        monkeypatch.setattr(
            mod, "run_project_command", lambda client, project, cmd: missing
        )

        result = mod.read_handoff(_FakeClient(), "demo")
        assert result["exit_code"] == 0
        assert result["stdout"] == "(no handoff plan)"

    def test_permission_error_is_not_masked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")

        denied = {
            "exit_code": 1,
            "stdout": "",
            "stderr": "cat: .ai-bridge/current-plan.md: Permission denied",
        }
        monkeypatch.setattr(
            mod, "run_project_command", lambda client, project, cmd: denied
        )

        result = mod.read_handoff(_FakeClient(), "demo")
        assert result["exit_code"] == 1
        assert "Permission denied" in result["stderr"]
