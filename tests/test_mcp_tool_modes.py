"""Tests for MCP tool mode visibility."""

from __future__ import annotations

import pytest

from examples.mcp_server.tool_modes import (
    CHATGPT_BLOCKED_TOOLS,
    DEFAULT_TOOL_MODE,
    TOOL_NAMES_BY_MODE,
    ToolModeError,
    get_chatgpt_safe_tools,
    get_tool_mode,
    is_chatgpt_safe_mode,
    should_register_tool,
    tools_for_mode,
)


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
        monkeypatch.delenv("MCP_CHATGPT_SAFE_MODE", raising=False)
        assert not is_chatgpt_safe_mode()

    def test_safe_mode_enabled(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_CHATGPT_SAFE_MODE", "true")
        assert is_chatgpt_safe_mode()

    def test_blocked_tools_excludes_agent_launch(self):
        assert "project_run_opencode" in CHATGPT_BLOCKED_TOOLS
        assert "project_run_mimo" in CHATGPT_BLOCKED_TOOLS
        assert "project_run_agent" in CHATGPT_BLOCKED_TOOLS

    def test_blocked_tools_excludes_docker(self):
        for name in ("docker_exec", "docker_compose_up", "docker_compose_down", "docker_prune"):
            assert name in CHATGPT_BLOCKED_TOOLS

    def test_blocked_tools_excludes_write_mutations(self):
        for name in ("workspace_file_write", "workspace_file_edit", "workspace_apply_patch",
                       "project_apply_patch"):
            assert name in CHATGPT_BLOCKED_TOOLS

    def test_safe_tools_include_readonly(self):
        safe = get_chatgpt_safe_tools()
        for name in ("health", "tools_manifest", "job_status", "read_file", "repo_status"):
            assert name in safe

    def test_safe_tools_include_testlint(self):
        safe = get_chatgpt_safe_tools()
        for name in ("run_tests", "run_lint", "project_run_pytest", "project_run_ruff"):
            assert name in safe

    def test_safe_tools_exclude_blocked(self):
        safe = get_chatgpt_safe_tools()
        assert len(safe & CHATGPT_BLOCKED_TOOLS) == 0

    def test_safe_mode_filters_registration(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        monkeypatch.setenv("MCP_CHATGPT_SAFE_MODE", "true")
        assert should_register_tool("health")
        assert should_register_tool("read_file")
        assert not should_register_tool("project_run_opencode")
        assert not should_register_tool("docker_exec")
        assert not should_register_tool("workspace_file_write")

    def test_safe_mode_off_allows_all_chatgpt(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        monkeypatch.delenv("MCP_CHATGPT_SAFE_MODE", raising=False)
        assert should_register_tool("project_run_opencode")
        assert should_register_tool("docker_exec")
