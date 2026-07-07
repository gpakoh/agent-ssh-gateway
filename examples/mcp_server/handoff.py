"""Handoff helpers for .ai-bridge planning workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gateway_client import GatewayClient, resolve_file_path
from write_modes import assert_handoff_write_allowed

AI_BRIDGE_DIR = ".ai-bridge"
CURRENT_PLAN = f"{AI_BRIDGE_DIR}/current-plan.md"
AGENT_STATUS = f"{AI_BRIDGE_DIR}/agent-status.md"
IMPLEMENTATION_DIFF = f"{AI_BRIDGE_DIR}/implementation-diff.patch"


def build_handoff_plan(task: str, agent: str = "opencode", notes: str | None = None) -> str:
    """Build a standard handoff plan body."""
    timestamp = datetime.now(UTC).isoformat()
    notes_section = f"\n## Additional notes\n\n{notes.strip()}\n" if notes else ""

    return (
        f"# Agent handoff plan\n\n"
        f"Created: {timestamp}\n"
        f"Target agent: {agent}\n\n"
        f"## Task\n\n{task.strip()}\n\n"
        f"## Implementation contract\n\n"
        f"- Work in small, reviewable steps.\n"
        f"- Keep changes scoped to the task.\n"
        f"- Do not overwrite this plan unless explicitly asked.\n"
        f"- Update `.ai-bridge/agent-status.md` with progress, touched files, tests, "
        f"blockers, and next review notes.\n"
        f"- Save final review diff to `.ai-bridge/implementation-diff.patch` when "
        f"practical.\n"
        f"- Do not expose secrets, tokens, private keys, or `.env` contents.\n"
        f"{notes_section}"
        f"## Suggested local agent prompt\n\n"
        f"Read `.ai-bridge/current-plan.md` and execute it in small, reviewable "
        f"steps.\n"
        f"After each meaningful change, update `.ai-bridge/agent-status.md`.\n"
    )


def write_handoff_plan(
    client: GatewayClient,
    *,
    task: str,
    agent: str = "opencode",
    notes: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Write .ai-bridge/current-plan.md through the gateway file API."""
    assert_handoff_write_allowed()
    plan = build_handoff_plan(task=task, agent=agent, notes=notes)
    resolved = resolve_file_path(CURRENT_PLAN)
    return client.write_file(resolved, plan, session_id=session_id, mode="overwrite")


def read_handoff(client: GatewayClient, *, session_id: str | None = None) -> dict[str, Any]:
    """Read standard .ai-bridge handoff files."""
    files: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for name, path in {
        "current_plan": CURRENT_PLAN,
        "agent_status": AGENT_STATUS,
        "implementation_diff": IMPLEMENTATION_DIFF,
    }.items():
        try:
            resolved = resolve_file_path(path)
            files[name] = client.read_file(resolved, session_id=session_id)
        except Exception as exc:
            errors[name] = str(exc)

    return {
        "files": files,
        "errors": errors,
        "paths": {
            "current_plan": CURRENT_PLAN,
            "agent_status": AGENT_STATUS,
            "implementation_diff": IMPLEMENTATION_DIFF,
        },
    }


def show_handoff_status(client: GatewayClient, *, session_id: str | None = None) -> dict[str, Any]:
    """Return compact handoff status."""
    handoff = read_handoff(client, session_id=session_id)
    files = handoff["files"]
    errors = handoff["errors"]

    return {
        "has_current_plan": "current_plan" in files,
        "has_agent_status": "agent_status" in files,
        "has_implementation_diff": "implementation_diff" in files,
        "missing_or_unreadable": sorted(errors),
        "paths": handoff["paths"],
    }
