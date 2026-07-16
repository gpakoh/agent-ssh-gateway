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
        """Auto agent selects opencode from allowed → should be blocked."""
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(task_json=_make_task_json())
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID)

    def test_auto_agent_opencode_selected(self):
        """Auto agent → opencode selected → should be blocked."""
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(task_json=_make_task_json())
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID)

    def test_explicit_opencode_agent_disabled(self):
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(task_json=_make_task_json(agent="opencode"))
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID)

    def test_explicit_mimo_agent_disabled(self):
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(
            task_json=_make_task_json(agent="mimo", worktree_path="/tmp/wt"),
        )
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID)

    def test_no_allowed_backends(self):
        rc = _make_run_cmd(task_json=_make_task_json(allowed=[]))
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"

    def test_no_task_json(self):
        rc = _make_run_cmd(task_json="")
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"

    def test_mimo_without_worktree_path(self):
        """mimo backend → blocked (before worktree check)."""
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(task_json=_make_task_json(agent="mimo"))
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID)

    def test_opencode_without_current_plan(self):
        """opencode backend → blocked (before plan check)."""
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(task_json=_make_task_json(), current_plan="")
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID)


# ── project_run_agent: router enabled ───────────────────────────────────────


class TestProjectRunAgentEnabled:
    def _router(self, enabled: bool = True) -> AgentBackendRouter:
        r = AgentBackendRouter(
            fallback_order=["opencode", "mimo"],
            enabled=enabled,
        )
        return r

    def test_opencode_selected_when_available(self):
        """Router selects opencode → should be blocked."""
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(task_json=_make_task_json())
        r = self._router()
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID, router=r)

    def test_mimo_selected_when_opencode_cooldown(self):
        """Router selects mimo (opencode cooldown) → should be blocked."""
        import time

        from command_policy import CommandPolicyError

        from examples.mcp_server.agent_backend_router import BackendStatus

        rc = _make_run_cmd(
            task_json=_make_task_json(worktree_path="/tmp/wt"),
        )
        r = self._router()
        r._backends["opencode"].status = BackendStatus.COOLDOWN
        r._backends["opencode"].cooldown_until = time.time() + 3600
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID, router=r)

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
        """Router disabled → auto → opencode → blocked."""
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(task_json=_make_task_json())
        r = self._router(enabled=False)
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID, router=r)

    def test_record_result_not_called_when_blocked(self):
        """Blocking happens before record_result."""
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(task_json=_make_task_json())
        r = self._router()
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        # record_result should NOT have been called
        assert r._backends["opencode"].status.value == "available"

    def test_selected_backend_not_in_allowed(self):
        rc = _make_run_cmd(
            task_json=_make_task_json(agent="auto", allowed=["mimo"]),
        )
        r = self._router()
        # Router may select opencode, which is blocked
        # Or it may select mimo, which is also blocked
        # Either way, should raise CommandPolicyError
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
            # Router selected mimo (first in allowed) or opencode
            pass


# ── Integration: router + tool flow ─────────────────────────────────────────


class TestAgentToolIntegration:
    def test_auto_agent_opencode_flow(self):
        """Router disabled → auto → opencode → blocked."""
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(task_json=_make_task_json())
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID)

    def test_fallback_to_mimo(self):
        """Router enabled, opencode cooldown → mimo → blocked."""
        import time

        from command_policy import CommandPolicyError

        from examples.mcp_server.agent_backend_router import BackendStatus

        rc = _make_run_cmd(
            task_json=_make_task_json(worktree_path="/tmp/wt"),
        )
        r = AgentBackendRouter(fallback_order=["opencode", "mimo"], enabled=True)
        r._backends["opencode"].status = BackendStatus.COOLDOWN
        r._backends["opencode"].cooldown_until = time.time() + 3600
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID, router=r)

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


# ── project_run_agent: C3 blocking ─────────────────────────────────────────


class TestProjectRunAgentC3Blocking:
    def test_opencode_blocked(self):
        """project_run_agent with agent=opencode must raise CommandPolicyError."""
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(task_json=_make_task_json(agent="opencode"))
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID)

    def test_mimo_blocked(self):
        """project_run_agent with agent=mimo must raise CommandPolicyError."""
        from command_policy import CommandPolicyError

        rc = _make_run_cmd(
            task_json=_make_task_json(agent="mimo", worktree_path="/tmp/wt"),
        )
        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID)

    def test_opencode_blocked_before_execution(self):
        """Blocking must happen before script execution (after task.json read)."""
        from command_policy import CommandPolicyError

        call_count = 0

        def counting_run(project, command):
            nonlocal call_count
            call_count += 1
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        task_json = _make_task_json(agent="opencode")
        rc = _make_run_cmd(task_json=task_json, exit_code=0)
        # Override to count actual executions
        original_side_effect = rc.side_effect

        def counting_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original_side_effect(*args, **kwargs)

        rc.side_effect = counting_side_effect

        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_agent(rc, project="test", task_id=TASK_ID)
        # call_count=1 means only task.json was read, no script executed
        assert call_count == 1, f"Expected 1 call (task.json read), got {call_count}"
