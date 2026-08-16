"""OpenCode runner MCP tool — execute handoff tasks via OpenCode CLI.

Thin wrapper around agent_tools.py's opencode script-building logic (the
single, dedicated entrypoint -- not routed through the agent backend
router's cooldown/fallback selection like run_agent). Runs with
--dangerously-skip-permissions -- opencode's own internal safety
confirmations are disabled for unattended execution. Gated by write-mode
(assert_handoff_write_allowed, checked by gateway_run_opencode before
calling into this module) and by tool-mode registration (excluded from
mcp_client/mcp_client_write's tool sets -- see tool_modes.py).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from examples.mcp_server.agent_paths import managed_workspace_path, task_dir
from examples.mcp_server.agent_tasks import validate_base_ref, validate_task_id
from examples.mcp_server.agent_tools import (
    _agent_submission_key,
    _build_opencode_script,
    _isolated_worktree_error,
    _now_iso,
    _read_current_plan,
    _read_task_json,
    _resolve_project_root,
    _task_string_list,
)


def project_run_opencode(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
    run_script: Callable[[str, str], dict[str, Any]] | None = None,
    run_script_async: Callable[[str, str, str], dict[str, Any]] | None = None,
    async_submit: bool = False,
) -> dict[str, Any]:
    """Execute an existing handoff task via OpenCode CLI on the SSH target.

    Args:
        run_cmd: callable(project, command) that executes a single shell
            command -- used only to read current-plan.md, never for the
            multi-line opencode script itself (that needs run_script:
            run_cmd's underlying execute-argv path shlex.splits its
            command string, which mangles a multi-line script's own
            syntax -- if/then/fi, heredocs -- into broken argv).
        project: project name under MCP_GATEWAY_PROJECT_ROOT
        task_id: validated .ai-bridge task ID (must exist in tasks/)
        model: optional model override (e.g., "gpt-4o")
        run_script: callable(project, script) for multi-line bash scripts,
            required for the synchronous (async_submit=False) path
        run_script_async: callable(project, script, submission_key) -> {"job_id": ...},
            submits without waiting -- required when async_submit=True
        async_submit: submit and return a job_id immediately instead of
            waiting for the full run (fleet mode: launch several agents
            without blocking, poll each job_id independently)

    Returns:
        dict with keys: task_id, status, exit_code, stdout, stderr,
        started_at, finished_at (async: status="running", job_id set,
        exit_code/stdout/stderr/finished_at are None/empty until polled)
    """
    validate_task_id(task_id)

    started_at = _now_iso()
    td = task_dir(project, task_id)

    plan = _read_current_plan(run_cmd, project, task_id)
    if not plan:
        return {
            "task_id": task_id,
            "status": "error",
            "error": "current-plan.md not found — write task plan first",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    project_root = _resolve_project_root(project)
    task_json = _read_task_json(run_cmd, project, task_id)
    managed_path = managed_workspace_path(project, task_id)
    managed_clone = managed_path is not None
    worktree_path = managed_path or ((task_json or {}).get("worktree_path") or "").strip() or None
    isolation_error = _isolated_worktree_error(project_root, worktree_path)
    if isolation_error:
        return {
            "task_id": task_id,
            "status": "error",
            "error": isolation_error,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }
    try:
        raw_base_ref = (task_json or {}).get("base_ref")
        validate_base_ref(raw_base_ref)
        base_ref = raw_base_ref if isinstance(raw_base_ref, str) and raw_base_ref else None
        allowed_files = _task_string_list(task_json or {}, "allowed_files")
        forbidden_files = _task_string_list(task_json or {}, "forbidden_files")
        required_checks = _task_string_list(task_json or {}, "required_checks")
    except (TypeError, ValueError) as exc:
        return {
            "task_id": task_id,
            "status": "error",
            "error": str(exc),
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }
    cmd = _build_opencode_script(
        td,
        task_id,
        model,
        project_root=project_root,
        worktree_path=worktree_path,
        allowed_files=allowed_files,
        forbidden_files=forbidden_files,
        required_checks=required_checks,
        managed_clone=managed_clone,
        base_ref=base_ref,
    )

    if async_submit:
        if run_script_async is None:
            return {
                "task_id": task_id,
                "status": "error",
                "error": "async_submit=True requires an async submit callable",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": None,
            }
        submitted = run_script_async(project, cmd, _agent_submission_key(project, task_id))
        return {
            "task_id": task_id,
            "status": "running",
            "job_id": submitted.get("job_id"),
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": None,
        }

    result = (run_script or run_cmd)(project, cmd)
    exit_code = result.get("exit_code")

    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    if project_root:
        try:
            from examples.mcp_server.mcp_client_tools import _redact_project_root

            stdout = _redact_project_root(stdout, project_root)
            stderr = _redact_project_root(stderr, project_root)
        except Exception:
            pass  # redaction failure must not hide a real result

    return {
        "task_id": task_id,
        "status": "needs-review"
        if exit_code == 0
        else "blocked"
        if exit_code == 76
        else "resource-exhausted"
        if exit_code == 137
        else "failed"
        if exit_code is not None
        else "error",
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
