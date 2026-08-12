"""Agent Handoff v2 adapter.

client and _agent_router are resolved through the server module at call
time: tests patch examples.mcp_server.server.client and expect the
patched client here. _split_lines is imported from the gateway adapter.

Tools are registered explicitly via register_all() (called by server.py
after runtime.set_mcp) instead of import-time decorator side effects.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_tasks import (
    archive_agent_task as _archive_agent_task,
)
from agent_tasks import (
    list_agent_tasks as _list_agent_tasks,
)
from agent_tasks import (
    read_agent_task_file as _read_agent_task_file,
)
from agent_tasks import (
    write_agent_task as _write_agent_task,
)
from agent_tools import project_run_agent as _project_run_agent
from mcp_client_tools import run_project_command
from opencode_tools import project_run_opencode as _project_run_opencode

from examples.mcp_server.fleet_runtime import get_fleet_runtime
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.adapters.gateway import _split_lines
from examples.mcp_server.mcp_infra.tool_registry import register_tool, run_tool, run_tool_async


def _server_client():
    return server_attr("client")


def _server_agent_router():
    return server_attr("_agent_router")


def _split_scope_patterns(value: str | None) -> list[str] | None:
    """Parse allowed/forbidden file patterns from the MCP string surface.

    Newlines are canonical, but a comma-separated single line is accepted as
    a convenience because the public MCP schema exposes these fields as strings
    rather than arrays. Keep this parser scope-only: required_checks may
    legitimately contain commas and must remain newline-separated.
    """
    lines = _split_lines(value)
    if lines is None:
        return None
    patterns: list[str] = []
    for line in lines:
        patterns.extend(part.strip() for part in line.split(",") if part.strip())
    return patterns


# ── Agent Handoff v2 tools ──────────────────────────────────────────


def gateway_write_agent_task(
    project: str,
    task_id: str,
    agent: str,
    task: str,
    scope: str = "",
    allowed_files: str | None = None,
    forbidden_files: str | None = None,
    required_checks: str | None = None,
    acceptance_criteria: str | None = None,
    commit_message: str | None = None,
    constraints: str | None = None,
    worktree_path: str | None = None,
) -> dict[str, Any]:
    """Write task.json + current-plan.md to .ai-bridge/tasks/<task_id>/."""

    def _fn() -> dict[str, Any]:
        return _write_agent_task(
            # Script transport (sh + stdin), NOT run_project_command: the
            # generated command is a multi-line heredoc script that
            # shlex.split() would shred (live: mkdir saw 'cat', '>', 'JEOF'
            # as separate argv entries). execute_project_script pipes it
            # verbatim to a bare `sh` -- same shape, correct semantics.
            lambda p, s: _server_client().execute_project_script(p, s),
            project=project,
            task_id=task_id,
            agent=agent,
            task=task,
            scope=scope,
            allowed_files=_split_scope_patterns(allowed_files),
            forbidden_files=_split_scope_patterns(forbidden_files),
            required_checks=_split_lines(required_checks),
            acceptance_criteria=_split_lines(acceptance_criteria),
            commit_message=commit_message,
            constraints=constraints,
            worktree_path=worktree_path,
        )

    return run_tool(
        tool="write_agent_task",
        title="Write agent task",
        fn=_fn,
        success_text="Wrote agent task.",
    )


def gateway_read_agent_status(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-status.md."""
    return run_tool(
        tool="read_agent_status",
        title="Read agent status",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            filename="agent-status.md",
        ),
        success_text="Read agent status.",
    )


def gateway_read_agent_report(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-report.md."""
    return run_tool(
        tool="read_agent_report",
        title="Read agent report",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            filename="agent-report.md",
        ),
        success_text="Read agent report.",
    )


def gateway_read_agent_diff(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/implementation-diff.patch."""
    return run_tool(
        tool="read_agent_diff",
        title="Read agent diff",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            filename="implementation-diff.patch",
        ),
        success_text="Read agent diff.",
    )


def gateway_list_agent_tasks(project: str) -> dict[str, Any]:
    """List task directories under .ai-bridge/tasks/."""
    return run_tool(
        tool="list_agent_tasks",
        title="List agent tasks",
        fn=lambda: _list_agent_tasks(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
        ),
        success_text="Listed agent tasks.",
    )


def gateway_archive_agent_task(project: str, task_id: str) -> dict[str, Any]:
    """Move .ai-bridge/tasks/<task_id>/ -> .ai-bridge/archive/<task_id>/."""
    return run_tool(
        tool="archive_agent_task",
        title="Archive agent task",
        fn=lambda: _archive_agent_task(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
        ),
        success_text="Archived agent task.",
    )


async def gateway_run_opencode(
    project: str,
    task_id: str,
    model: str | None = None,
    async_submit: bool = False,
) -> dict[str, Any]:
    """Execute an existing handoff task via OpenCode CLI.

    When durable fleet admission is enabled, async submissions acquire a
    shared Postgres lease before the blocking gateway call. The blocking HTTP
    path runs in a worker thread; asyncpg stays on the FastMCP event loop.
    """
    from write_modes import assert_handoff_write_allowed

    assert_handoff_write_allowed()

    def _submit() -> dict[str, Any]:
        return _project_run_opencode(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            model=model,
            run_script=lambda p, s: _server_client().execute_project_script(p, s),
            run_script_async=lambda p, s, k: _server_client().execute_project_script_async(
                p, s, k
            ),
            async_submit=async_submit,
        )

    async def _fn() -> dict[str, Any]:
        if async_submit:
            fleet = await get_fleet_runtime()
            if fleet is not None:
                return await fleet.submit(
                    project=project,
                    task_id=task_id,
                    submit_sync=_submit,
                )
        return await asyncio.to_thread(_submit)

    return await run_tool_async(
        tool="run_opencode",
        title="Run opencode task",
        fn=_fn,
        success_text="Submitted opencode task.",
    )


async def gateway_run_agent(
    project: str,
    task_id: str,
    model: str | None = None,
    async_submit: bool = False,
) -> dict[str, Any]:
    """Execute a handoff task via the backend router with optional fleet admission."""
    from write_modes import assert_handoff_write_allowed

    assert_handoff_write_allowed()

    def _submit() -> dict[str, Any]:
        return _project_run_agent(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            model=model,
            router=_server_agent_router(),
            run_script=lambda p, s: _server_client().execute_project_script(p, s),
            run_script_async=lambda p, s, k: _server_client().execute_project_script_async(
                p, s, k
            ),
            async_submit=async_submit,
        )

    async def _fn() -> dict[str, Any]:
        if async_submit:
            fleet = await get_fleet_runtime()
            if fleet is not None:
                return await fleet.submit(
                    project=project,
                    task_id=task_id,
                    submit_sync=_submit,
                )
        return await asyncio.to_thread(_submit)

    return await run_tool_async(
        tool="run_agent",
        title="Run agent task (router)",
        fn=_fn,
        success_text="Submitted agent task via router.",
    )

def register_all() -> None:
    register_tool("write_agent_task")(gateway_write_agent_task)
    register_tool("read_agent_status")(gateway_read_agent_status)
    register_tool("read_agent_report")(gateway_read_agent_report)
    register_tool("read_agent_diff")(gateway_read_agent_diff)
    register_tool("list_agent_tasks")(gateway_list_agent_tasks)
    register_tool("archive_agent_task")(gateway_archive_agent_task)
    register_tool("run_opencode")(gateway_run_opencode)
    register_tool("run_agent")(gateway_run_agent)
