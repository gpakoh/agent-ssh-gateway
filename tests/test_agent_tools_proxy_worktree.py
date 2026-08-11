"""Tests for agent_tools._build_opencode_script — live-proxy lease/report
loop and per-worker git worktree isolation.

Mirrors quart-platform/opencode-adapter's proxy pattern: lease a live
proxy from the provider before the run (GET /proxy?format=provider), give
it back on rate limit (POST /proxy/report -> provider cooldown). Pure
script-building logic, no real opencode binary — same style as
test_opencode_runner_argv.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from examples.mcp_server.agent_tools import (
    PROXY_LIMIT_MARKERS,
    _build_opencode_script,
    _proxy_report_script_lines,
    _read_task_json,
)

TD = ".ai-bridge/tasks/a12345678901"
TASK_ID = "a12345678901"
PROVIDER = "http://proxy-parser-worker:8080/proxy?format=provider"


class TestReadTaskJsonLenient:
    """task.json is optional metadata for run_opencode (worktree_path).
    Corrupt/unrelated stdout must not crash the run; run_agent still
    fails closed because it treats {} as "task.json not found"."""

    def test_corrupt_stdout_returns_empty(self):
        rc = MagicMock(return_value={"exit_code": 0, "stdout": "# Plan", "stderr": ""})
        assert _read_task_json(rc, "proj", TASK_ID) == {}

    def test_empty_stdout_returns_empty(self):
        rc = MagicMock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})
        assert _read_task_json(rc, "proj", TASK_ID) == {}

    def test_valid_json_parsed(self):
        import json

        rc = MagicMock(
            return_value={
                "exit_code": 0,
                "stdout": json.dumps({"worktree_path": "/wt/x"}),
                "stderr": "",
            }
        )
        assert _read_task_json(rc, "proj", TASK_ID) == {"worktree_path": "/wt/x"}


class TestProxyReportScriptLines:
    def test_report_url_derived_from_provider_base(self):
        lines = "\n".join(_proxy_report_script_lines(PROVIDER, "5"))
        assert "http://proxy-parser-worker:8080/proxy/report" in lines

    def test_report_contract_has_proxy_and_retry_after(self):
        lines = "\n".join(_proxy_report_script_lines(PROVIDER, "5"))
        assert "proxy_url" in lines
        assert "retry_after_seconds" in lines
        assert "json.dumps" in lines

    def test_all_limit_markers_in_grep_pattern(self):
        lines = "\n".join(_proxy_report_script_lines(PROVIDER, "5"))
        for marker in PROXY_LIMIT_MARKERS:
            assert marker in lines

    def test_report_is_best_effort(self):
        lines = "\n".join(_proxy_report_script_lines(PROVIDER, "5"))
        assert "except Exception" in lines

    def test_retry_after_fallback_default(self):
        lines = "\n".join(_proxy_report_script_lines(PROVIDER, "5"))
        assert "RETRY_AFTER=300" in lines


class TestBuildOpencodeScriptProxy:
    def test_fetch_and_report_wired_when_env_set(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", PROVIDER)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "PROXYFETCH_EOF" in script
        assert "PROXYREPORT_EOF" in script
        assert "proxy/report" in script
        assert "RATE_LIMITED=1" in script

    def test_no_proxy_lines_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "PROXYFETCH_EOF" not in script
        assert "proxy/report" not in script
        assert "RATE_LIMITED=1" not in script

    def test_opencode_output_captured_for_detection(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", PROVIDER)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert '> "$td/opencode-output.log" 2>&1' in script
        assert 'cat "$td/opencode-output.log"' in script

    def test_rate_limited_status_wins_over_failed(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", PROVIDER)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert 'echo "Status: rate-limited" > "$td/agent-status.md"' in script


class TestBuildOpencodeScriptWorktree:
    def test_worktree_added_when_path_provided(self):
        script = _build_opencode_script(
            TD, TASK_ID, None, project_root="/srv/proj", worktree_path="/srv/proj/.ai-bridge/worktrees/a12345678901"
        )
        assert "git worktree add --detach" in script
        assert 'cd "$wt" || exit 1' in script

    def test_relative_worktree_resolved_against_project_root(self):
        script = _build_opencode_script(
            TD, TASK_ID, None, project_root="/srv/proj", worktree_path="../agent-worktrees/a12345678901"
        )
        assert "wt='/srv/agent-worktrees/a12345678901'" in script

    def test_no_worktree_lines_without_path(self):
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "git worktree add" not in script

    def test_td_absolute_in_worktree_mode(self):
        script = _build_opencode_script(
            TD, TASK_ID, None, project_root="/srv/proj", worktree_path="/srv/proj/wt"
        )
        assert "td='/srv/proj/.ai-bridge/tasks/a12345678901'" in script

    def test_td_absolute_with_project_root_without_worktree(self):
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "td='/srv/proj/.ai-bridge/tasks/a12345678901'" in script
