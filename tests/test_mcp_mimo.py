"""Tests for Mimo runner MCP tool — mimo_tools module."""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

import pytest

from examples.mcp_server.mimo_tools import project_run_mimo, validate_model

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
TASK_ID = "2026-06-25-mimo-task-opencode"


def _fake_run_cmd(project: str, command: str) -> dict:
    return {"stdout": command, "stderr": "", "exit_code": 0}


def _capture_command(project: str, cmd: str) -> dict:
    _capture_command.last_cmd = cmd
    return {"stdout": cmd, "stderr": "", "exit_code": 0}


class TestModelValidation:
    def test_valid_models(self):
        for m in [
            "big-pickle",
            "zen/big-pickle",
            "claude-sonnet-4",
            "provider:model",
            "a:b@1.2+c-d",
        ]:
            assert validate_model(m) == m

    def test_none_passes_through(self):
        assert validate_model(None) is None

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="Invalid model name"):
            validate_model("Big Pickle")

    def test_rejects_shell_chars(self):
        for bad in ["x; rm -rf /", "$(whoami)", "`id`", "foo && bar", "foo|bar"]:
            with pytest.raises(ValueError, match="Invalid model name"):
                validate_model(bad)

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="Invalid model name"):
            validate_model("")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="Invalid model name"):
            validate_model("x" * 81)


class TestCommandConstruction:
    def test_contains_dangerously_skip_permissions(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert _capture_command.last_cmd is not None
        assert "--dangerously-skip-permissions" in _capture_command.last_cmd

    def test_contains_task_id(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert TASK_ID in _capture_command.last_cmd

    def test_contains_worktree_root_guard(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "MCP_GATEWAY_WORKTREE_ROOT" in _capture_command.last_cmd

    def test_contains_agent_mimo_guard(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "agent is not mimo" in _capture_command.last_cmd

    def test_contains_do_not_commit(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "Do not commit" in _capture_command.last_cmd

    def test_contains_do_not_push(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "do not push" in _capture_command.last_cmd

    def test_contains_work_only_inside_worktree(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "Work only inside" in _capture_command.last_cmd

    def test_contains_diff_from_worktree(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert 'git -C "$WORKTREE_REAL" diff' in _capture_command.last_cmd

    def test_contains_linked_worktree_check(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "git-common-dir" in _capture_command.last_cmd

    def test_contains_mimo_binary_discovery(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "MIMO_BIN" in _capture_command.last_cmd

    def test_contains_model_flag_when_provided(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID, model="big-pickle")
        assert "--model 'big-pickle'" in _capture_command.last_cmd

    def test_default_model_used_when_none(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID, model=None)
        assert "--model" in _capture_command.last_cmd
        assert "ollama-gen/gemma4:26b" in _capture_command.last_cmd

    def test_env_var_overrides_default_model(self, monkeypatch):
        monkeypatch.setenv("MIMO_DEFAULT_MODEL", "ollama-check/gemma4:26b")
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID, model=None)
        assert "ollama-check/gemma4:26b" in _capture_command.last_cmd
        assert "ollama-gen/gemma4:26b" not in _capture_command.last_cmd

    def test_explicit_model_still_works(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID, model="big-pickle")
        assert "--model 'big-pickle'" in _capture_command.last_cmd

    def test_contains_no_proxy_exports(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "NO_PROXY" in _capture_command.last_cmd
        assert "no_proxy" in _capture_command.last_cmd
        assert "MIMO_EXTRA_NO_PROXY" in _capture_command.last_cmd

    def test_no_proxy_contains_local_ips(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "10.0.1.103" in _capture_command.last_cmd
        assert "10.0.0.3" in _capture_command.last_cmd

    def test_uses_absolute_paths(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "$PROJECT_REAL/$td/" in _capture_command.last_cmd

    def test_worktree_path_guard_present(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "worktree_path not set" in _capture_command.last_cmd


class TestProjectRunMimo:
    def test_invalid_task_id_raises(self):
        with pytest.raises(ValueError, match="Invalid task_id"):
            project_run_mimo(_fake_run_cmd, project="test", task_id="bad")

    def test_invalid_model_raises_before_run_cmd(self):
        call_log = []

        def tracking_run_cmd(p, c):
            call_log.append(c)
            return {"stdout": "", "stderr": "", "exit_code": 0}

        with pytest.raises(ValueError, match="Invalid model name"):
            project_run_mimo(tracking_run_cmd, project="test", task_id=TASK_ID, model="Big Pickle")
        assert len(call_log) == 0, "run_cmd should not be called with invalid model"

    def test_accepted_task_id_returns_structured_result(self):
        result = project_run_mimo(_fake_run_cmd, project="test", task_id=TASK_ID)
        assert result["task_id"] == TASK_ID

    def test_returns_structured_result_keys(self):
        result = project_run_mimo(_fake_run_cmd, project="test", task_id=TASK_ID)
        for key in (
            "task_id",
            "status",
            "exit_code",
            "stdout",
            "stderr",
            "started_at",
            "finished_at",
        ):
            assert key in result, f"missing key: {key}"

    def test_status_needs_review_on_zero_exit(self):
        def ok_run_cmd(p, c):
            return {"stdout": "", "stderr": "", "exit_code": 0}

        result = project_run_mimo(ok_run_cmd, project="test", task_id=TASK_ID)
        assert result["status"] == "needs-review"

    def test_status_failed_on_nonzero_exit(self):
        def fail_run_cmd(p, c):
            return {"stdout": "", "stderr": "error", "exit_code": 1}

        result = project_run_mimo(fail_run_cmd, project="test", task_id=TASK_ID)
        assert result["status"] == "failed"


MIMO_BIN = os.getenv("MIMO_BIN") or shutil.which("mimo") or "/root/.mimocode/bin/mimo"


class TestToolRegistration:
    def test_registered_in_chatgpt_mode(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib
        import sys

        example_dir = EXAMPLE_DIR
        monkeypatch.syspath_prepend(str(example_dir))
        sys.modules.pop("tool_modes", None)
        tm = importlib.import_module("tool_modes")
        assert tm.should_register_tool("project_run_mimo") is True

    def test_visible_in_tools_for_chatgpt(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib
        import sys

        example_dir = EXAMPLE_DIR
        monkeypatch.syspath_prepend(str(example_dir))
        sys.modules.pop("tool_modes", None)
        tm = importlib.import_module("tool_modes")
        tools = tm.tools_for_mode()
        assert "project_run_mimo" in tools


class TestServerTool:
    @pytest.mark.skipif(
        not importlib.util.find_spec("mcp"),
        reason="mcp package not installed; only available with optional dependencies",
    )
    def test_tool_function_can_be_imported(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib
        import sys

        example_dir = EXAMPLE_DIR
        monkeypatch.syspath_prepend(str(example_dir))
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "handoff")
        monkeypatch.setenv("GITEA_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        for name in list(sys.modules):
            if "mimo_tools" in name or "mcp_server" in name or "tool_modes" in name:
                sys.modules.pop(name, None)
        server = importlib.import_module("server")
        tool = getattr(server, "gateway_project_run_mimo", None)
        assert tool is not None, "gateway_project_run_mimo not found in server module"
