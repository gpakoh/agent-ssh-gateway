"""Tests for MCP tool mode visibility."""

from __future__ import annotations

import os

import pytest

from examples.mcp_server.tool_modes import (
    DEFAULT_TOOL_MODE,
    MCP_CLIENT_BLOCKED_TOOLS,
    MCP_CLIENT_WRITE_BLOCKED_TOOLS,
    TOOL_NAMES_BY_MODE,
    ToolModeError,
    get_mcp_client_safe_tools,
    get_mcp_client_write_tools,
    get_tool_mode,
    is_mcp_client_safe_mode,
    should_register_tool,
    tools_for_mode,
)
from examples.mcp_server.tool_scopes import get_required_scopes


@pytest.fixture(autouse=True)
def _clean_tool_mode_env():
    """Remove MCP_GATEWAY_TOOL_MODE before each test so the default is used."""
    os.environ.pop("MCP_GATEWAY_TOOL_MODE", None)
    yield


class TestToolModeDefaults:
    def test_default_mode_is_standard(self):
        assert DEFAULT_TOOL_MODE == "standard"

    def test_get_tool_mode_default(self):
        mode = get_tool_mode()
        assert mode == "standard"

    def test_all_modes_have_health(self):
        for mode in TOOL_NAMES_BY_MODE:
            assert "health" in TOOL_NAMES_BY_MODE[mode]

    def test_standard_includes_session_listing(self):
        assert "list_sessions" in TOOL_NAMES_BY_MODE["standard"]
        assert "read_file" in TOOL_NAMES_BY_MODE["standard"]
        assert "repo_status" in TOOL_NAMES_BY_MODE["standard"]

    def test_minimal_excludes_read_repo_jobwait(self):
        minimal = TOOL_NAMES_BY_MODE["minimal"]
        assert "read_file" not in minimal
        assert "repo_status" not in minimal
        assert "wait_job" not in minimal
        assert "list_sessions" not in minimal

    def test_minimal_includes_health_execute_jobs(self):
        minimal = TOOL_NAMES_BY_MODE["minimal"]
        assert "health" in minimal
        assert "execute_restricted" in minimal
        assert "job_status" in minimal
        assert "job_result" in minimal


class TestShouldRegisterTool:
    def test_health_in_all_modes(self):
        for mode in TOOL_NAMES_BY_MODE:
            assert should_register_tool("health", mode)

    def test_list_sessions_not_in_minimal(self):
        assert not should_register_tool("list_sessions", "minimal")
        assert should_register_tool("list_sessions", "standard")

    def test_read_file_not_in_minimal(self):
        assert not should_register_tool("read_file", "minimal")
        assert should_register_tool("read_file", "standard")

    def test_wait_job_not_in_minimal(self):
        assert not should_register_tool("wait_job", "minimal")
        assert should_register_tool("wait_job", "standard")

    def test_unknown_tool_returns_false(self):
        for mode in TOOL_NAMES_BY_MODE:
            assert not should_register_tool("write_file", mode)

    def test_unknown_mode_raises(self):
        with pytest.raises(ToolModeError):
            should_register_tool("health", mode="nonexistent")  # type: ignore[arg-type]


class TestGetToolMode:
    def test_env_var_minimal(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "minimal")
        assert get_tool_mode() == "minimal"

    def test_env_var_full(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "full")
        assert get_tool_mode() == "full"

    def test_env_var_case_insensitive(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "FULL")
        assert get_tool_mode() == "full"

    def test_env_var_invalid_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "bogus")
        with pytest.raises(ToolModeError, match="Invalid MCP_GATEWAY_TOOL_MODE"):
            get_tool_mode()


class TestToolsForMode:
    def test_tools_for_minimal(self):
        names = tools_for_mode("minimal")
        assert "health" in names
        assert "list_sessions" not in names

    def test_tools_for_standard(self):
        names = tools_for_mode("standard")
        assert "health" in names
        assert "list_sessions" in names
        assert "read_file" in names

    def test_tools_for_full(self):
        names = tools_for_mode("full")
        assert "repo_status" in names

    def test_tools_for_none_uses_default(self):
        mode = tools_for_mode()
        assert mode == TOOL_NAMES_BY_MODE[DEFAULT_TOOL_MODE]


# ---------------------------------------------------------------------------
# ChatGPT safe mode
# ---------------------------------------------------------------------------


class TestChatGPTSafeMode:
    def test_safe_mode_default_off(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MCP_CLIENT_SAFE_MODE", raising=False)
        assert not is_mcp_client_safe_mode()

    def test_safe_mode_enabled(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
        assert is_mcp_client_safe_mode()

    def test_blocked_tools_excludes_agent_launch(self):
        assert "run_opencode" in MCP_CLIENT_BLOCKED_TOOLS
        assert "run_agent" in MCP_CLIENT_BLOCKED_TOOLS

    def test_blocked_tools_excludes_docker(self):
        for name in ("docker_exec", "docker_compose_up", "docker_compose_down", "docker_prune"):
            assert name in MCP_CLIENT_BLOCKED_TOOLS

    def test_blocked_tools_excludes_write_mutations(self):
        for name in (
            "workspace_file_write",
            "workspace_file_edit",
            "workspace_apply_patch",
            "apply_patch",
        ):
            assert name in MCP_CLIENT_BLOCKED_TOOLS

    def test_safe_tools_include_readonly(self):
        safe = get_mcp_client_safe_tools()
        for name in ("health", "tools_manifest", "job_status", "read_file", "repo_status"):
            assert name in safe

    def test_safe_tools_include_testlint(self):
        safe = get_mcp_client_safe_tools()
        for name in ("run_tests", "run_lint", "run_pytest", "run_ruff"):
            assert name in safe

    def test_safe_tools_exclude_blocked(self):
        safe = get_mcp_client_safe_tools()
        assert len(safe & MCP_CLIENT_BLOCKED_TOOLS) == 0

    def test_supervisor_integration_tools_are_write_mode_only(self):
        supervisor_tools = {
            "supervisor_integrate_file",
            "supervisor_recover_integrations",
        }
        assert supervisor_tools <= TOOL_NAMES_BY_MODE["mcp_client_write"]
        for mode, names in TOOL_NAMES_BY_MODE.items():
            if mode == "mcp_client_write":
                continue
            assert supervisor_tools.isdisjoint(names), mode
        assert supervisor_tools.isdisjoint(get_mcp_client_safe_tools())

    def test_supervisor_integration_tools_require_admin_scope(self):
        for name in (
            "supervisor_integrate_file",
            "supervisor_recover_integrations",
        ):
            assert get_required_scopes(name) == ["mcp:admin"]

    def test_gitea_pr_create_is_write_mode_only_and_admin_scoped(self):
        assert "gitea_create_pull_request" in TOOL_NAMES_BY_MODE["mcp_client_write"]
        assert "gitea_create_pull_request" not in TOOL_NAMES_BY_MODE["mcp_client"]
        assert "gitea_create_pull_request" not in get_mcp_client_safe_tools()
        assert get_required_scopes("gitea_create_pull_request") == ["mcp:admin"]

    def test_safe_mode_filters_registration(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
        monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
        assert should_register_tool("health")
        assert should_register_tool("read_file")
        assert not should_register_tool("run_opencode")
        assert not should_register_tool("docker_exec")
        assert not should_register_tool("workspace_file_write")

    def test_safe_mode_off_allows_all_mcp_client(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
        monkeypatch.delenv("MCP_CLIENT_SAFE_MODE", raising=False)
        assert should_register_tool("run_opencode")
        assert should_register_tool("docker_exec")
        assert should_register_tool("workspace_file_write")
        assert should_register_tool("workspace_file_edit")
        assert should_register_tool("workspace_apply_patch")

    def test_safe_mode_on_blocks_workspace_write(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
        monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
        assert not should_register_tool("workspace_file_write")
        assert not should_register_tool("workspace_file_edit")
        assert not should_register_tool("workspace_apply_patch")


# ---------------------------------------------------------------------------
# mcp_client_write mode -- project read/write + git commit/push +
# agent launch + Docker/Postgres admin (FULL rights: nothing blocked).
# ---------------------------------------------------------------------------


class TestMcpClientWriteMode:
    def test_execute_argv_present(self):
        write_tools = TOOL_NAMES_BY_MODE["mcp_client_write"]
        assert "execute_argv" in write_tools

    def test_git_write_tools_present(self):
        write_tools = TOOL_NAMES_BY_MODE["mcp_client_write"]
        assert "git_add" in write_tools
        assert "git_commit" in write_tools
        assert "git_create_branch" in write_tools
        assert "git_push" in write_tools

    def test_git_write_tools_absent_from_every_other_mode(self):
        """Git mutation tools must never leak into any other mode; the
        protected-master workflow is intentionally available only through the
        explicit write mode."""
        for mode, names in TOOL_NAMES_BY_MODE.items():
            if mode == "mcp_client_write":
                continue
            assert "git_add" not in names, mode
            assert "git_commit" not in names, mode
            assert "git_create_branch" not in names, mode
            assert "git_push" not in names, mode

    def test_workspace_write_tools_present(self):
        write_tools = TOOL_NAMES_BY_MODE["mcp_client_write"]
        for name in (
            "workspace_file_write",
            "workspace_file_edit",
            "workspace_apply_patch",
            "workspace_preview_write",
            "workspace_preview_edit",
            "workspace_preview_patch",
            "workspace_verify",
            "apply_patch",
        ):
            assert name in write_tools

    def test_project_inspection_and_tests_still_present(self):
        write_tools = TOOL_NAMES_BY_MODE["mcp_client_write"]
        for name in (
            "health",
            "read_file",
            "repo_status",
            "run_tests",
            "run_lint",
            "git_status",
            "git_diff",
        ):
            assert name in write_tools

    def test_docker_admin_allowed(self):
        """mcp_client_write now carries FULL rights: Docker admin, agent
        launch, handoff/agent-task writes are all reachable (the user
        granted the ChatGPT-connected client full access)."""
        write_tools = TOOL_NAMES_BY_MODE["mcp_client_write"]
        for name in (
            "docker_exec",
            "docker_run",
            "docker_rmi",
            "docker_volume_rm",
            "docker_stop",
            "docker_restart",
            "docker_compose_up",
            "docker_compose_down",
            "docker_prune",
        ):
            assert name in write_tools

    def test_agent_launch_allowed(self):
        write_tools = TOOL_NAMES_BY_MODE["mcp_client_write"]
        assert "run_opencode" in write_tools
        assert "run_agent" in write_tools

    def test_handoff_and_agent_task_write_allowed(self):
        write_tools = TOOL_NAMES_BY_MODE["mcp_client_write"]
        assert "write_handoff_plan" in write_tools
        assert "write_agent_task" in write_tools
        assert "archive_agent_task" in write_tools

    def test_get_mcp_client_write_tools_matches_mode_entry(self):
        assert get_mcp_client_write_tools() == frozenset(TOOL_NAMES_BY_MODE["mcp_client_write"])

    def test_write_blocked_tools_disjoint_from_write_mode(self):
        write_tools = TOOL_NAMES_BY_MODE["mcp_client_write"]
        assert len(write_tools & MCP_CLIENT_WRITE_BLOCKED_TOOLS) == 0

    def test_registration_via_should_register_tool(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client_write")
        assert should_register_tool("execute_argv")
        assert should_register_tool("git_push")
        assert should_register_tool("git_commit")
        assert should_register_tool("git_create_branch")
        assert should_register_tool("workspace_file_write")
        assert should_register_tool("read_file")
        assert should_register_tool("docker_exec")
        assert should_register_tool("run_agent")

    def test_plain_mcp_client_mode_is_unaffected(self):
        """The new mode must not change plain "mcp_client" mode's own
        tool set or safe-mode filtering in any way."""
        mcp_client_tools = TOOL_NAMES_BY_MODE["mcp_client"]
        assert "execute_argv" not in mcp_client_tools
        assert "git_add" not in mcp_client_tools
        assert "git_commit" not in mcp_client_tools
        assert "git_create_branch" not in mcp_client_tools
        assert "git_push" not in mcp_client_tools
        safe = get_mcp_client_safe_tools()
        assert len(safe & MCP_CLIENT_BLOCKED_TOOLS) == 0
