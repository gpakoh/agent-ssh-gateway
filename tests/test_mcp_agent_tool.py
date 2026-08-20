"""Tests for agent_tools — gateway_project_run_agent routing.

project_run_agent's opencode dispatch (and opencode_tools.py's
project_run_opencode) run real --dangerously-skip-permissions execution
-- the earlier "C3" hard block was deliberately lifted so run_agent/
run_opencode can actually launch opencode, including async_submit=True
for fleet mode (launch several agents without blocking, poll each job_id
independently). Both tools remain gated by write-mode
(assert_handoff_write_allowed) and by tool-mode registration
(run_agent/run_opencode excluded from mcp_client/mcp_client_write's tool
sets) at the server.py/tool_modes.py layer -- not tested here.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from examples.mcp_server.agent_backend_router import AgentBackendRouter
from examples.mcp_server.agent_tools import CommandPolicyError as AgentCommandPolicyError
from examples.mcp_server.agent_tools import project_run_agent

CommandPolicyError = AgentCommandPolicyError

TASK_ID = "test-agent-001"
TASKS_REL = ".ai-bridge/tasks"
TD = f"{TASKS_REL}/{TASK_ID}"

def test_read_agent_log_redacts_obvious_secrets(monkeypatch):
    import examples.mcp_server.mcp_infra.adapters.agent as agent_adapter

    monkeypatch.setattr(
        agent_adapter,
        "_read_agent_log_tail",
        lambda *args, **kwargs: {
            "stdout": "password=hunter2\ntoken=abc123\nworking\n",
            "stderr": "",
            "exit_code": 0,
            "truncated": False,
        },
    )

    result = agent_adapter.gateway_read_agent_log("test", TASK_ID, tail_lines=20)

    assert result["ok"] is True
    assert "hunter2" not in result["result"]["stdout"]
    assert "abc123" not in result["result"]["stdout"]
    assert "[REDACTED]" in result["result"]["stdout"]
    assert result["meta"]["redacted"] is True



# ── helpers ─────────────────────────────────────────────────────────────────


def _make_task_json(agent: str = "auto", allowed: list[str] | None = None, **extra) -> str:
    if allowed is None:
        allowed = ["opencode"]
    data: dict[str, object] = {
        "agent": agent,
        "allowed_backends": allowed,
        "worktree_path": "../agent-worktrees/test-agent-001",
    }
    data.update(extra)
    return json.dumps(data)


def _make_run_cmd(
    task_json: str = "{}",
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    current_plan: str = "# Plan\n\n1. Do the thing",
) -> MagicMock:
    """Create a run_cmd mock that returns task.json for cat and plan for other.

    Also serves as the fallback script executor (project_run_agent falls
    back to run_cmd when run_script isn't provided) -- its catch-all
    branch doesn't inspect command content, so it works for both the
    single-line plan/task.json reads and the multi-line opencode script.
    """

    def fake_run_cmd(project: str, command: str) -> dict:
        if command.startswith("ls -ld -- "):
            return {"exit_code": 0, "stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": ""}
        # startswith("cat "), not a bare substring check -- the generated
        # opencode script itself legitimately contains the substring
        # "current-plan.md" (it checks for the file's existence), so a
        # naive "in command" match would misfire on the script execution
        # call too, not just the actual `cat .../current-plan.md` read.
        if command.startswith("cat ") and "task.json" in command:
            return {"exit_code": 0, "stdout": task_json, "stderr": ""}
        if command.startswith("cat ") and "current-plan.md" in command:
            return {"exit_code": 0, "stdout": current_plan, "stderr": ""}
        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}

    return MagicMock(side_effect=fake_run_cmd)


def _make_run_script_async(job_id: str = "job-async-1") -> MagicMock:
    return MagicMock(return_value={"job_id": job_id})


# ── project_run_agent: router disabled ──────────────────────────────────────


class TestProjectRunAgentDisabled:
    def test_auto_agent_executes_opencode(self):
        """Auto agent selects opencode from allowed and actually runs it."""
        rc = _make_run_cmd(task_json=_make_task_json(), exit_code=0, stdout="done")
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "needs-review"
        assert result["exit_code"] == 0
        assert result["stdout"] == "done"

    def test_explicit_opencode_agent_executes(self):
        rc = _make_run_cmd(task_json=_make_task_json(agent="opencode"), exit_code=0)
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "needs-review"

    def test_nonzero_exit_reports_failed(self):
        rc = _make_run_cmd(task_json=_make_task_json(), exit_code=1, stderr="boom")
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "failed"
        assert result["exit_code"] == 1

    def test_no_allowed_backends(self):
        rc = _make_run_cmd(task_json=_make_task_json(allowed=[]))
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"

    def test_no_task_json(self):
        rc = _make_run_cmd(task_json="")
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"

    def test_unsupported_backend_errors(self):
        """An allowed_backends entry that isn't opencode is neither blocked
        nor executed — it falls through to an "unsupported backend" error.
        """
        rc = _make_run_cmd(
            task_json=_make_task_json(agent="custom-backend", allowed=["custom-backend"]),
        )
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"
        assert "unsupported backend" in result["error"]

    def test_opencode_without_current_plan_errors(self):
        """No current-plan.md → error, before any script is built/executed."""
        rc = _make_run_cmd(task_json=_make_task_json(), current_plan="")
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"
        assert "current-plan.md" in result["error"]

    def test_non_string_base_ref_errors_instead_of_escaping_wrapper(self):
        rc = _make_run_cmd(task_json=_make_task_json(base_ref=123))
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "error"
        assert "base_ref must be a string or None" in result["error"]


# ── project_run_agent: router enabled ───────────────────────────────────────


class TestProjectRunAgentEnabled:
    def _router(self, enabled: bool = True) -> AgentBackendRouter:
        r = AgentBackendRouter(
            fallback_order=["opencode"],
            enabled=enabled,
        )
        return r

    def test_opencode_selected_and_executed(self):
        rc = _make_run_cmd(task_json=_make_task_json(), exit_code=0, stdout="ok")
        r = self._router()
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        assert result["status"] == "needs-review"
        assert result["stdout"] == "ok"

    def test_blocked_when_opencode_unavailable(self):
        import time

        from examples.mcp_server.agent_backend_router import BackendStatus

        rc = _make_run_cmd(task_json=_make_task_json())
        r = self._router()
        r._backends["opencode"].status = BackendStatus.COOLDOWN
        r._backends["opencode"].cooldown_until = time.time() + 3600
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        assert result["status"] == "blocked"

    def test_router_disabled_uses_direct_agent(self):
        rc = _make_run_cmd(task_json=_make_task_json(), exit_code=0)
        r = self._router(enabled=False)
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        assert result["status"] == "needs-review"

    def test_record_result_called_after_sync_execution(self):
        """The router's cooldown tracking IS fed by synchronous runs."""
        rc = _make_run_cmd(task_json=_make_task_json(), exit_code=0)
        r = self._router()
        project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        assert r._backends["opencode"].status.value == "available"

    def test_selected_backend_not_in_allowed(self):
        """Router selects opencode (its only configured backend) but
        task.json's allowed_backends doesn't include it -- must error out,
        not execute anyway.
        """
        rc = _make_run_cmd(
            task_json=_make_task_json(agent="auto", allowed=["custom-other-backend"]),
        )
        r = self._router()
        result = project_run_agent(rc, project="test", task_id=TASK_ID, router=r)
        assert result["status"] == "error"
        assert "not in allowed_backends" in result["error"]


# ── project_run_agent: async_submit (fleet mode) ────────────────────────────


class TestProjectRunAgentAsyncSubmit:
    def test_returns_job_id_immediately(self):
        rc = _make_run_cmd(task_json=_make_task_json())
        run_script_async = _make_run_script_async("job-42")
        result = project_run_agent(
            rc, project="test", task_id=TASK_ID, async_submit=True, run_script_async=run_script_async
        )
        assert result["status"] == "running"
        assert result["job_id"] == "job-42"
        assert result["exit_code"] is None
        assert result["finished_at"] is None
        run_script_async.assert_called_once()
        submission_key = run_script_async.call_args.args[2]
        assert submission_key.startswith("task:test-")
        assert submission_key.endswith(f":{TASK_ID}")

    def test_run_cmd_never_called_for_script_execution(self):
        """Only task.json + current-plan.md are read via run_cmd; the actual
        opencode script goes through run_script_async, not run_cmd.
        """
        calls: list[str] = []

        def fake_run_cmd(project, command):
            calls.append(command)
            if command.startswith("ls -ld -- "):
                return {"exit_code": 0, "stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": ""}
            if "task.json" in command:
                return {"exit_code": 0, "stdout": _make_task_json(), "stderr": ""}
            if "current-plan.md" in command:
                return {"exit_code": 0, "stdout": "# Plan", "stderr": ""}
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        run_script_async = _make_run_script_async("job-99")
        result = project_run_agent(
            fake_run_cmd,
            project="test",
            task_id=TASK_ID,
            async_submit=True,
            run_script_async=run_script_async,
        )
        assert result["job_id"] == "job-99"
        assert sum(call.startswith("cat ") for call in calls) == 2
        assert all(
            call.startswith("ls -ld -- ") or call.startswith("cat ")
            for call in calls
        )
        assert any(call.startswith("cat ") and "task.json" in call for call in calls)
        assert any(call.startswith("cat ") and "current-plan.md" in call for call in calls)
        assert all("--dangerously-skip-permissions" not in call for call in calls)

    def test_missing_run_script_async_errors(self):
        """async_submit=True with no async callable provided must error,
        not silently fall back to a blocking run.
        """
        rc = _make_run_cmd(task_json=_make_task_json())
        result = project_run_agent(rc, project="test", task_id=TASK_ID, async_submit=True)
        assert result["status"] == "error"
        assert "async_submit" in result["error"]

    def test_router_record_result_not_called_for_async(self):
        """No completion callback exists for async-submitted jobs -- the
        router must not be told anything happened yet.
        """
        rc = _make_run_cmd(task_json=_make_task_json())
        r = AgentBackendRouter(fallback_order=["opencode"], enabled=True)
        run_script_async = _make_run_script_async("job-1")
        project_run_agent(
            rc, project="test", task_id=TASK_ID, router=r, async_submit=True, run_script_async=run_script_async
        )
        assert r._backends["opencode"].status.value == "available"

    def test_no_current_plan_still_errors_before_submitting(self):
        rc = _make_run_cmd(task_json=_make_task_json(), current_plan="")
        run_script_async = _make_run_script_async()
        result = project_run_agent(
            rc, project="test", task_id=TASK_ID, async_submit=True, run_script_async=run_script_async
        )
        assert result["status"] == "error"
        run_script_async.assert_not_called()


# ── project_run_agent: host-path redaction (sync path) ──────────────────────


class TestProjectRunAgentRedaction:
    def test_project_root_redacted_from_stdout_stderr(self, monkeypatch):
        project_root = "/media/1TB/Python/web_ssh/web-ssh-gateway/workspace/test"
        rc = _make_run_cmd(
            task_json=_make_task_json(),
            exit_code=0,
            stdout=f"running in {project_root}/subdir",
            stderr=f"warning at {project_root}",
        )

        class _FakeRegistry:
            def project_info(self, project):
                return {"root": project_root}

        monkeypatch.setattr(
            "app.workspace.registry.get_registry", lambda: _FakeRegistry()
        )

        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert project_root not in result["stdout"]
        assert project_root not in result["stderr"]
        assert result["stdout"] == "running in ./subdir"


# ── Integration: router + tool flow ─────────────────────────────────────────


class TestAgentToolIntegration:
    def test_auto_agent_opencode_flow(self):
        rc = _make_run_cmd(task_json=_make_task_json(), exit_code=0)
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "needs-review"

    def test_opencode_tools_executes(self):
        """opencode_tools.project_run_opencode runs for real too (the
        dedicated single-shot entrypoint, not routed through the backend
        router)."""
        from examples.mcp_server.opencode_tools import project_run_opencode

        rc = _make_run_cmd(exit_code=0, stdout="ok")
        result = project_run_opencode(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "needs-review"
        assert result["stdout"] == "ok"

    def test_agent_and_direct_opencode_share_submission_identity(self):
        from examples.mcp_server.opencode_tools import project_run_opencode

        rc = _make_run_cmd(task_json=_make_task_json())
        agent_submit = _make_run_script_async("job-agent")
        direct_submit = _make_run_script_async("job-direct")

        project_run_agent(
            rc,
            project="test",
            task_id=TASK_ID,
            async_submit=True,
            run_script_async=agent_submit,
        )
        project_run_opencode(
            rc,
            project="test",
            task_id=TASK_ID,
            async_submit=True,
            run_script_async=direct_submit,
        )

        assert agent_submit.call_args.args[2] == direct_submit.call_args.args[2]


# ── project_run_agent: execution ordering ───────────────────────────────────


class TestProjectRunAgentExecutionOrdering:
    def test_plan_read_before_script_execution(self):
        """task.json, then current-plan.md, then the opencode script itself
        -- in that order, and the script only runs once a plan exists."""
        calls: list[str] = []

        def counting_run(project, command):
            calls.append(command)
            if command.startswith("ls -ld -- "):
                return {"exit_code": 0, "stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": ""}
            if "task.json" in command:
                return {"exit_code": 0, "stdout": _make_task_json(agent="opencode"), "stderr": ""}
            if "current-plan.md" in command:
                return {"exit_code": 0, "stdout": "# Plan", "stderr": ""}
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        result = project_run_agent(counting_run, project="test", task_id=TASK_ID)
        assert result["status"] == "needs-review"
        task_cat = next(
            i for i, call in enumerate(calls)
            if call.startswith("cat ") and "task.json" in call
        )
        plan_probe = next(
            i for i, call in enumerate(calls)
            if call.startswith("ls -ld -- ") and "current-plan.md" in call
        )
        plan_cat = next(
            i for i, call in enumerate(calls)
            if call.startswith("cat ") and "current-plan.md" in call
        )
        script_idx = next(
            i for i, call in enumerate(calls)
            if "--dangerously-skip-permissions" in call
        )
        assert task_cat < plan_probe < plan_cat < script_idx


# ── project_run_agent: script cd's into the project root ────────────────────


class TestProjectRunAgentScriptCwd:
    """Regression: confirmed live via a real MCP run_agent call --
    execute_argv (the sync dispatch path, via run_script=execute_project_script)
    sets cwd server-side, so the generated script's relative $td references
    happened to resolve correctly there, but execute_raw (the async dispatch
    path, via run_script_async=execute_project_script_async) has no cwd
    concept at all -- the script ran from the SSH session's own default
    directory (its home dir), and every relative $td reference silently
    resolved to the wrong place. A real async_submit=True run_agent call
    failed with "current-plan.md not found" despite the file genuinely
    existing at the right path. The script must cd into the absolute
    project root itself, so it's correct regardless of which dispatch path
    invoked it.
    """

    def test_generated_script_cds_into_project_root(self, monkeypatch):
        monkeypatch.setattr(
            "app.workspace.registry.get_registry",
            lambda: type("R", (), {"project_info": lambda self, p: {"root": "/abs/project/root"}})(),
        )
        calls: list[str] = []

        def counting_run(project, command):
            calls.append(command)
            if command.startswith("ls -ld -- "):
                return {"exit_code": 0, "stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": ""}
            if "task.json" in command:
                return {"exit_code": 0, "stdout": _make_task_json(), "stderr": ""}
            if "current-plan.md" in command:
                return {"exit_code": 0, "stdout": "# Plan", "stderr": ""}
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        project_run_agent(counting_run, project="test", task_id=TASK_ID)
        script = calls[-1]
        assert script.startswith("cd '/abs/project/root' || exit 1")

    def test_managed_workspace_env_rejects_task_supplied_worktree(self, monkeypatch):
        """Regression: managed mode silently replaced a valid user-supplied
        worktree_path with the executor-owned managed path.  Fail-closed:
        error out instead of silently substituting."""
        monkeypatch.setenv("MCP_AGENT_WORKSPACE_ROOT", "/var/lib/mcp-agent/workspaces")
        monkeypatch.setenv("MCP_AGENT_STATE_ROOT", "/var/lib/mcp-agent/state")
        monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", "/var/lib/mcp-agent/sources")
        base_ref = "b" * 40
        monkeypatch.setattr(
            "app.workspace.registry.get_registry",
            lambda: type("R", (), {"project_info": lambda self, p: {"root": "/abs/project/root"}})(),
        )
        rc = _make_run_cmd(
            task_json=_make_task_json(
                worktree_path="/abs/project/root/attacker-chosen", base_ref=base_ref
            )
        )
        run_script_async = _make_run_script_async("job-managed-1")

        result = project_run_agent(
            rc,
            project="test",
            task_id=TASK_ID,
            async_submit=True,
            run_script_async=run_script_async,
        )

        assert result["status"] == "error"
        assert "worktree_path" in result["error"].lower() or "managed" in result["error"].lower()
        run_script_async.assert_not_called()

    def test_no_cd_when_project_root_unresolvable(self, monkeypatch):
        """Registry lookup failure must not crash the whole call -- just
        skip the cd (matching the pre-fix, still-correct-for-sync behavior)."""
        monkeypatch.setattr(
            "app.workspace.registry.get_registry",
            lambda: (_ for _ in ()).throw(RuntimeError("no registry")),
        )
        rc = _make_run_cmd(task_json=_make_task_json(), exit_code=0)
        result = project_run_agent(rc, project="test", task_id=TASK_ID)
        assert result["status"] == "needs-review"

    def test_non_managed_mode_honors_user_worktree_path(self):
        """Regression: when MCP_AGENT_WORKSPACE_ROOT is NOT set, a valid
        user-supplied worktree_path from task.json must appear in the
        generated script."""
        user_wt = "/tmp/external-agent-workspace"
        captured: list[str] = []

        def counting_run(project, command):
            captured.append(command)
            if command.startswith("ls -ld -- "):
                return {"exit_code": 0, "stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": ""}
            if "task.json" in command:
                return {"exit_code": 0, "stdout": _make_task_json(worktree_path=user_wt), "stderr": ""}
            if "current-plan.md" in command:
                return {"exit_code": 0, "stdout": "# Plan", "stderr": ""}
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        project_run_agent(counting_run, project="test", task_id=TASK_ID)
        script = captured[-1]
        assert user_wt in script

    def test_async_submit_script_also_cds_into_project_root(self, monkeypatch):
        monkeypatch.setattr(
            "app.workspace.registry.get_registry",
            lambda: type("R", (), {"project_info": lambda self, p: {"root": "/abs/project/root"}})(),
        )
        rc = _make_run_cmd(task_json=_make_task_json())
        run_script_async = _make_run_script_async("job-cwd-1")
        project_run_agent(
            rc, project="test", task_id=TASK_ID, async_submit=True, run_script_async=run_script_async
        )
        submitted_script = run_script_async.call_args[0][1]
        assert submitted_script.startswith("cd '/abs/project/root' || exit 1")


# ── gateway_write_agent_task: script transport, not argv ────────────────────


class TestGatewayWriteAgentTaskScriptTransport:
    """Task payloads use shell-safe base64 transport through script stdin."""

    @staticmethod
    def _decoded_payload(script: str, filename: str) -> str:
        line = next(
            line for line in script.splitlines() if line.endswith(f"/{filename}")
        )
        encoded = line.split("printf %s ", 1)[1].split(" | base64 -d", 1)[0]
        return base64.b64decode(encoded).decode("utf-8")

    def test_routes_through_execute_project_script(self, monkeypatch):
        import examples.mcp_server.server as server_mod
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_write_agent_task

        client = MagicMock()
        client.execute_project_script.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
        monkeypatch.setattr(server_mod, "client", client)

        result = gateway_write_agent_task(
            project="test", task_id=TASK_ID, agent="opencode", task="Do the thing"
        )

        assert result["result"]["exit_code"] == 0
        client.execute_project_script.assert_called_once()
        script = client.execute_project_script.call_args[0][1]
        assert 'mkdir -p "$td"' in script
        assert "if [ -L .ai-bridge ]; then exit 46; fi" in script
        assert f"if [ -L {TD} ]; then exit 46; fi" in script
        assert f"if [ -L {TD}/task.json ]; then exit 46; fi" in script
        contract = json.loads(self._decoded_payload(script, "task.json"))
        assert contract["task_id"] == TASK_ID
        assert contract["base_ref"] == ""
        assert "base-ref.txt" not in script
        assert "<< 'JEOF'" not in script
        client.execute_argv.assert_not_called()
        client.execute_project_command.assert_not_called()

    def test_forwards_base_ref_into_contract_and_base_ref_txt(self, monkeypatch):
        import examples.mcp_server.server as server_mod
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_write_agent_task

        client = MagicMock()
        client.execute_project_script.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
        monkeypatch.setattr(server_mod, "client", client)
        sha = "0" * 40

        result = gateway_write_agent_task(
            project="test",
            task_id=TASK_ID,
            agent="opencode",
            task="Do the thing",
            base_ref=sha,
        )

        assert result["result"]["exit_code"] == 0
        script = client.execute_project_script.call_args[0][1]
        contract = json.loads(self._decoded_payload(script, "task.json"))
        assert contract["base_ref"] == sha
        assert self._decoded_payload(script, "base-ref.txt") == sha

    def test_rejects_invalid_base_ref_before_script(self, monkeypatch):
        import examples.mcp_server.server as server_mod
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_write_agent_task

        client = MagicMock()
        client.execute_project_script.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
        monkeypatch.setattr(server_mod, "client", client)

        result = gateway_write_agent_task(
            project="test",
            task_id=TASK_ID,
            agent="opencode",
            task="Do the thing",
            base_ref="main",
        )

        assert result["ok"] is False
        assert "INVALID_INPUT" in result["error"]["code"]
        client.execute_project_script.assert_not_called()

    def test_comma_separated_scope_patterns_become_distinct_contract_entries(self, monkeypatch):
        """The MCP string surface must not silently turn ``a.py,b.py`` into
        one impossible allowed-files glob. Commas are accepted for scope
        patterns only; required checks remain newline-separated."""
        import examples.mcp_server.server as server_mod
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_write_agent_task

        client = MagicMock()
        client.execute_project_script.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
        monkeypatch.setattr(server_mod, "client", client)

        gateway_write_agent_task(
            project="test",
            task_id=TASK_ID,
            agent="opencode",
            task="Do the thing",
            allowed_files="a.py,b.py",
            forbidden_files="secret/**,parent/**",
            required_checks="python -c 'print(1, 2)'",
        )

        script = client.execute_project_script.call_args[0][1]
        contract = json.loads(self._decoded_payload(script, "task.json"))
        assert contract["allowed_files"] == ["a.py", "b.py"]
        assert contract["forbidden_files"] == ["secret/**", "parent/**"]
        assert contract["required_checks"] == ["python -c 'print(1, 2)'"]

    def test_plan_marker_and_shell_text_stay_data_not_script(self, monkeypatch):
        import examples.mcp_server.server as server_mod
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_write_agent_task

        client = MagicMock()
        client.execute_project_script.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
        monkeypatch.setattr(server_mod, "client", client)
        hostile = "before\nPEOF\nprintf PWNED >/tmp/should-not-run\nafter"

        gateway_write_agent_task(
            project="test",
            task_id=TASK_ID,
            agent="opencode",
            task="Do the thing",
            constraints=hostile,
        )

        script = client.execute_project_script.call_args[0][1]
        plan = self._decoded_payload(script, "current-plan.md")
        assert hostile in plan
        assert "printf PWNED" not in script
        assert "PEOF" not in script

    def test_scope_pattern_parser_preserves_newline_contract(self):
        from examples.mcp_server.mcp_infra.adapters.agent import _split_scope_patterns

        assert _split_scope_patterns("a.py\nb.py") == ["a.py", "b.py"]
        assert _split_scope_patterns("a.py, b.py\nc.py") == ["a.py", "b.py", "c.py"]
        assert _split_scope_patterns(None) is None


def test_agent_adapter_registers_run_agents_tool(monkeypatch):
    import examples.mcp_server.mcp_infra.adapters.agent as adapter

    registered: dict[str, object] = {}

    def fake_register(name: str):
        def decorator(func):
            registered[name] = func
            return func

        return decorator

    monkeypatch.setattr(adapter, "register_tool", fake_register)
    adapter.register_all()

    assert registered["run_agents"] is adapter.gateway_run_agents


class TestGatewayRunAgents:
    @pytest.fixture(autouse=True)
    def _handoff_write_mode(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "handoff")

    @pytest.mark.asyncio
    async def test_batch_uses_one_shared_sweep_and_durable_submit_per_task(self, monkeypatch):
        import examples.mcp_server.server as server_mod
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_run_agents

        fleet = MagicMock()
        fleet.sweep_bound_leases = AsyncMock(return_value=0)

        async def submit(**kwargs):
            return {
                "task_id": kwargs["task_id"],
                "status": "running",
                "job_id": f"job-{kwargs['task_id']}",
            }

        fleet.submit = AsyncMock(side_effect=submit)

        async def get_fleet():
            return fleet

        monkeypatch.setattr(
            "examples.mcp_server.mcp_infra.adapters.agent.get_fleet_runtime",
            get_fleet,
        )
        monkeypatch.setattr(server_mod, "client", MagicMock())

        result = await gateway_run_agents("test", "task-a\ntask-b")

        assert result["ok"] is True
        assert [item["task_id"] for item in result["result"]["items"]] == [
            "task-a",
            "task-b",
        ]
        fleet.sweep_bound_leases.assert_awaited_once()
        assert fleet.submit.await_count == 2
        assert all(
            call.kwargs["sweep_before_submit"] is False
            for call in fleet.submit.await_args_list
        )

    @pytest.mark.asyncio
    async def test_batch_admissions_start_concurrently(self, monkeypatch):
        import asyncio

        import examples.mcp_server.server as server_mod
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_run_agents

        both_started = asyncio.Event()
        started: set[str] = set()
        fleet = MagicMock()
        fleet.sweep_bound_leases = AsyncMock(return_value=0)

        async def submit(**kwargs):
            started.add(kwargs["task_id"])
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.5)
            return {"task_id": kwargs["task_id"], "status": "running", "job_id": "job"}

        fleet.submit = AsyncMock(side_effect=submit)

        async def get_fleet():
            return fleet

        monkeypatch.setattr(
            "examples.mcp_server.mcp_infra.adapters.agent.get_fleet_runtime",
            get_fleet,
        )
        monkeypatch.setattr(server_mod, "client", MagicMock())

        result = await gateway_run_agents("test", "task-a\ntask-b")

        assert result["ok"] is True
        assert started == {"task-a", "task-b"}

    @pytest.mark.asyncio
    async def test_batch_isolates_one_submission_error(self, monkeypatch):
        import examples.mcp_server.server as server_mod
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_run_agents

        fleet = MagicMock()
        fleet.sweep_bound_leases = AsyncMock(return_value=0)

        async def submit(**kwargs):
            if kwargs["task_id"] == "bad":
                raise RuntimeError("simulated admission failure")
            return {"task_id": kwargs["task_id"], "status": "running", "job_id": "job-ok"}

        fleet.submit = AsyncMock(side_effect=submit)

        async def get_fleet():
            return fleet

        monkeypatch.setattr(
            "examples.mcp_server.mcp_infra.adapters.agent.get_fleet_runtime",
            get_fleet,
        )
        monkeypatch.setattr(server_mod, "client", MagicMock())

        result = await gateway_run_agents("test", "good\nbad")
        items = result["result"]["items"]

        assert items[0]["status"] == "running"
        assert items[1]["task_id"] == "bad"
        assert items[1]["status"] == "error"

    @pytest.mark.asyncio
    async def test_batch_rejects_duplicate_task_ids_before_fleet_access(self, monkeypatch):
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_run_agents

        get_fleet = AsyncMock()
        monkeypatch.setattr(
            "examples.mcp_server.mcp_infra.adapters.agent.get_fleet_runtime",
            get_fleet,
        )

        result = await gateway_run_agents("test", "same\nsame")

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        get_fleet.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_async_run_agent_keeps_normal_pre_submit_sweep(self, monkeypatch):
        import examples.mcp_server.server as server_mod
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_run_agent

        fleet = MagicMock()
        fleet.submit = AsyncMock(
            return_value={"task_id": "single", "status": "running", "job_id": "job-single"}
        )

        async def get_fleet():
            return fleet

        monkeypatch.setattr(
            "examples.mcp_server.mcp_infra.adapters.agent.get_fleet_runtime",
            get_fleet,
        )
        monkeypatch.setattr(server_mod, "client", MagicMock())

        result = await gateway_run_agent("test", "single", async_submit=True)

        assert result["ok"] is True
        assert fleet.submit.await_args.kwargs["sweep_before_submit"] is True


class TestSplitCsvOrLines:
    """Regression: task-id string surface accepts newline and CSV forms."""

    def _fn(self):
        from examples.mcp_server.mcp_infra.adapters.gateway import _split_csv_or_lines

        return _split_csv_or_lines

    def test_none_returns_none(self):
        assert self._fn()(None) is None

    def test_empty_string_returns_none(self):
        assert self._fn()("") is None

    def test_whitespace_only_returns_none(self):
        assert self._fn()("   \n  \n  ") is None

    def test_single_id(self):
        assert self._fn()("task-1") == ["task-1"]

    def test_newline_separated(self):
        assert self._fn()("a\nb\nc") == ["a", "b", "c"]

    def test_csv_separated(self):
        assert self._fn()("a,b,c") == ["a", "b", "c"]

    def test_csv_with_spaces(self):
        assert self._fn()("a , b , c") == ["a", "b", "c"]

    def test_mixed_newline_and_comma(self):
        assert self._fn()("a,b\nc,d") == ["a", "b", "c", "d"]

    def test_trailing_delimiters_ignored(self):
        assert self._fn()("a,b,\n") == ["a", "b"]

    def test_empty_fragments_ignored(self):
        assert self._fn()("a,,b\n\n,c") == ["a", "b", "c"]

    def test_whitespace_stripped(self):
        assert self._fn()("  task-1  ,  task-2  ") == ["task-1", "task-2"]

    def test_single_with_trailing_comma(self):
        assert self._fn()("task-1,") == ["task-1"]

    def test_newline_csv_mixed_with_blanks(self):
        assert self._fn()("a, b\n\nc, d\n") == ["a", "b", "c", "d"]


class TestGatewayArchiveAgentTaskScriptTransport:
    def test_routes_archive_through_generated_script(self, monkeypatch):
        import examples.mcp_server.server as server_mod
        from examples.mcp_server.mcp_infra.adapters.agent import gateway_archive_agent_task

        client = MagicMock()
        client.execute_project_script.return_value = {
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }
        monkeypatch.setattr(server_mod, "client", client)

        result = gateway_archive_agent_task(project="test", task_id=TASK_ID)

        assert result["result"]["exit_code"] == 0
        client.execute_project_script.assert_called_once()
        script = client.execute_project_script.call_args.args[1]
        assert 'mv -T -- "$src" "$dst"' in script
        client.execute_project_command.assert_not_called()
        client.execute_argv.assert_not_called()
