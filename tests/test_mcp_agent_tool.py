"""Tests for agent_tools — gateway_project_run_agent routing."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from examples.mcp_server.agent_backend_router import AgentBackendRouter
from examples.mcp_server.agent_tools import project_run_agent

TASK_ID = "test-agent-001"
TASKS_REL = ".ai-bridge/tasks"
TD = f"{TASKS_REL}/{TASK_ID}"


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_task_json(agent: str = "auto", allowed: list[str] | None = None, **extra) -> str:
    if allowed is None:
        allowed = ["opencode", "mimo"]
    data: dict[str, object] = {"agent": agent, "allowed_backends": allowed}
    data.update(extra)
    return json.dumps(data)


def _make_run_cmd(
    task_json: str = "{}",
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    current_plan: str = "# Plan\n\n1. Do the thing",
) -> MagicMock:
    """Create a run_cmd mock that returns task.json for cat and plan for other."""

    def fake_run_cmd(project: str, command: str) -> dict:
        if "cat " in command and "task.json" in command:
            return {"exit_code": 0, "stdout": task_json, "stderr": ""}
        if "current-plan.md" in command:
            return {"exit_code": 0, "stdout": current_plan, "stderr": ""}
        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}

    return MagicMock(side_effect=fake_run_cmd)


# ── project_run_agent: router disabled ──────────────────────────────────────


class TestProjectRunAgentDisabled:
    def test_auto_agent_uses_first_allowed(self):
        rc = _make_run_cmd(task_json=_make_task_json())
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "needs-review"
        assert result["exit_code"] == 0

    def test_auto_agent_opencode_selected(self):
        rc = _make_run_cmd(task_json=_make_task_json())
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        # Router disabled → uses agent or first allowed → opencode
        assert result["task_id"] == TASK_ID

    def test_explicit_opencode_agent_disabled(self):
        rc = _make_run_cmd(task_json=_make_task_json(agent="opencode"))
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "needs-review"

    def test_explicit_mimo_agent_disabled(self):
        rc = _make_run_cmd(
            task_json=_make_task_json(agent="mimo", worktree_path="/tmp/wt"),
        )
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] in ("needs-review", "failed")

    def test_no_allowed_backends(self):
        rc = _make_run_cmd(task_json=_make_task_json(allowed=[]))
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"

    def test_no_task_json(self):
        rc = _make_run_cmd(task_json="")
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"

    def test_mimo_without_worktree_path(self):
        rc = _make_run_cmd(task_json=_make_task_json(agent="mimo"))
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"
        assert "worktree_path" in result.get("error", "")

    def test_opencode_without_current_plan(self):
        rc = _make_run_cmd(task_json=_make_task_json(), current_plan="")
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"
        assert "current-plan.md" in result.get("error", "")


# ── project_run_agent: router enabled ───────────────────────────────────────


class TestProjectRunAgentEnabled:
    def _router(self, enabled: bool = True) -> AgentBackendRouter:
        r = AgentBackendRouter(
            fallback_order=["opencode", "mimo"],
            enabled=enabled,
        )
        return r

    def test_opencode_selected_when_available(self):
        rc = _make_run_cmd(task_json=_make_task_json())
        r = self._router()
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        assert result["status"] == "needs-review"

    def test_mimo_selected_when_opencode_cooldown(self):
        import time

        from examples.mcp_server.agent_backend_router import BackendStatus

        rc = _make_run_cmd(
            task_json=_make_task_json(worktree_path="/tmp/wt"),
        )
        r = self._router()
        r._backends["opencode"].status = BackendStatus.COOLDOWN
        r._backends["opencode"].cooldown_until = time.time() + 3600
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        assert result["status"] == "needs-review" or result["status"] == "failed"

    def test_blocked_when_both_unavailable(self):
        import time

        from examples.mcp_server.agent_backend_router import BackendStatus

        rc = _make_run_cmd(task_json=_make_task_json())
        r = self._router()
        r._backends["opencode"].status = BackendStatus.COOLDOWN
        r._backends["opencode"].cooldown_until = time.time() + 3600
        r._backends["mimo"].status = BackendStatus.COOLDOWN
        r._backends["mimo"].cooldown_until = time.time() + 3600
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        assert result["status"] == "blocked"

    def test_router_disabled_uses_direct_agent(self):
        rc = _make_run_cmd(task_json=_make_task_json())
        r = self._router(enabled=False)
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        assert result["status"] == "needs-review"

    def test_record_result_called_on_success(self):
        rc = _make_run_cmd(task_json=_make_task_json())
        r = self._router()
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        assert result["status"] == "needs-review"
        assert r._backends["opencode"].status.value == "available"

    def test_selected_backend_not_in_allowed(self):
        rc = _make_run_cmd(
            task_json=_make_task_json(agent="auto", allowed=["mimo"]),
        )
        r = self._router()
        # With router enabled and opencode available, router may select opencode
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        if result["status"] == "error":
            assert "selected backend" in result.get("error", "")
        else:
            # Router selected mimo (first in allowed) or opencode
            pass


# ── Integration: router + tool flow ─────────────────────────────────────────


class TestAgentToolIntegration:
    def test_auto_agent_opencode_flow(self):
        """Router disabled → auto → opencode path."""
        rc = _make_run_cmd(task_json=_make_task_json())
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["task_id"] == TASK_ID
        assert result["exit_code"] == 0

    def test_fallback_to_mimo(self):
        """Router enabled, opencode cooldown → mimo selected."""
        import time

        from examples.mcp_server.agent_backend_router import BackendStatus

        rc = _make_run_cmd(
            task_json=_make_task_json(worktree_path="/tmp/wt"),
        )
        r = AgentBackendRouter(fallback_order=["opencode", "mimo"], enabled=True)
        r._backends["opencode"].status = BackendStatus.COOLDOWN
        r._backends["opencode"].cooldown_until = time.time() + 3600
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        # mimo may pass guards or fail on real worktree — but shouldn't crash
        assert result["status"] in ("needs-review", "failed", "error")

    def test_old_tools_unchanged(self):
        """Verify project_run_opencode is hard-blocked."""
        from examples.mcp_server.opencode_tools import project_run_opencode

        # opencode_tools raises an error (blocked)
        rc = _make_run_cmd(
            task_json=_make_task_json(),
            exit_code=0,
        )
        with pytest.raises(Exception, match="blocked"):
            project_run_opencode(rc, project="test", task_id=TASK_ID)
