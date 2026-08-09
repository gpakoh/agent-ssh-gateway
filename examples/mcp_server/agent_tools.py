"""Agent Backend Router MCP tool — routes task to OpenCode via router selection.

The router selects the backend based on availability and cooldown state.
When disabled, falls back to the task.json ``agent`` field.

Runs opencode with --dangerously-skip-permissions -- opencode's own
internal safety confirmations are disabled for unattended execution.
Gated by write-mode (assert_handoff_write_allowed, checked by the
gateway_run_agent/gateway_run_opencode MCP tool wrappers before calling
into this module) and by tool-mode registration (run_agent/run_opencode
are excluded from mcp_client/mcp_client_write's tool sets -- see
tool_modes.py) -- there is no confirmation flow or override *within*
this module itself.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from command_policy import CommandPolicyError  # noqa: F401 -- re-exported for tests

TASKS_REL_DIR = ".ai-bridge/tasks"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"


def _read_task_json(
    run_cmd: Callable[[str, str], dict[str, Any]],
    project: str,
    task_id: str,
) -> dict[str, Any]:
    import shlex

    cmd = f"cat {shlex.quote(f'{TASKS_REL_DIR}/{task_id}/task.json')}"
    result = run_cmd(project, cmd)
    raw = result.get("stdout", "")
    if not raw.strip():
        return {}
    return json.loads(raw)


def _read_current_plan(
    run_cmd: Callable[[str, str], dict[str, Any]],
    project: str,
    task_id: str,
) -> str | None:
    import shlex

    td = f"{TASKS_REL_DIR}/{task_id}"
    cmd = f"cat {shlex.quote(td)}/current-plan.md"
    result = run_cmd(project, cmd)
    return result.get("stdout", "").strip() or None


def _proxy_fetch_script_lines(provider_url: str, timeout: str) -> list[str]:
    """Bash lines that fetch a live proxy from OPENCODE_PROXY_PROVIDER_URL
    at *script execution time* on the SSH target (not at script-build time
    in this process) and export it for the opencode subprocess.

    Mirrors quart-platform/opencode-adapter's ProxySource._fetch_from_provider
    response-shape precedence exactly: {"proxy"|"https_proxy"|"url": "..."},
    then {"proxies": [...]}/a bare JSON array (first entry), then a bare
    text body. Empty/unreachable provider -> no proxy, opencode runs direct
    (same fail-open-to-no-proxy behavior as the adapter's static-pool
    fallback, just without a static pool here -- this is a single agent
    run, not a long-lived multi-instance service).
    """
    return [
        "",
        "# Fetch a live proxy from the provider container (OPENCODE_PROXY_PROVIDER_URL)",
        f"OPENCODE_PROXY_URL=$(python3 - {_shell_escape(provider_url)} {_shell_escape(timeout)} <<'PROXYFETCH_EOF'",
        "import json, sys, urllib.request",
        "",
        "url, timeout = sys.argv[1], float(sys.argv[2])",
        "try:",
        "    raw = urllib.request.urlopen(url, timeout=timeout).read().decode('utf-8', 'replace').strip()",
        "except Exception:",
        "    sys.exit(0)",
        "try:",
        "    data = json.loads(raw)",
        "except ValueError:",
        "    if raw:",
        "        print(raw)",
        "    sys.exit(0)",
        "if isinstance(data, dict):",
        "    for key in ('proxy', 'https_proxy', 'url'):",
        "        value = data.get(key)",
        "        if isinstance(value, str) and value.strip():",
        "            print(value.strip())",
        "            sys.exit(0)",
        "    proxies = data.get('proxies')",
        "    if isinstance(proxies, list) and proxies:",
        "        print(str(proxies[0]).strip())",
        "        sys.exit(0)",
        "elif isinstance(data, list) and data:",
        "    print(str(data[0]).strip())",
        "PROXYFETCH_EOF",
        ")",
        'if [ -n "$OPENCODE_PROXY_URL" ]; then',
        '  export HTTP_PROXY="$OPENCODE_PROXY_URL" HTTPS_PROXY="$OPENCODE_PROXY_URL"',
        '  export http_proxy="$OPENCODE_PROXY_URL" https_proxy="$OPENCODE_PROXY_URL"',
        '  export NO_PROXY="localhost,127.0.0.1" no_proxy="localhost,127.0.0.1"',
        '  echo "Using live proxy: $OPENCODE_PROXY_URL" >> "$td/agent-status.md"',
        "fi",
        "",
    ]


def _build_opencode_script(td: str, task_id: str, model: str | None) -> str:
    opencode_flags = "--dangerously-skip-permissions"
    if model:
        opencode_flags += f" --model {_shell_escape(model)}"

    proxy_provider_url = os.environ.get("OPENCODE_PROXY_PROVIDER_URL", "").strip()
    proxy_timeout = os.environ.get("OPENCODE_PROXY_PROVIDER_TIMEOUT", "5").strip() or "5"

    parts = [
        f"td='{td}'",
        'mkdir -p "$td"',
        'echo "Status: running" > "$td/agent-status.md"',
        "OPCODE_BIN=$(command -v opencode 2>/dev/null || echo '/root/.opencode/bin/opencode')",
    ]
    if proxy_provider_url:
        parts.extend(_proxy_fetch_script_lines(proxy_provider_url, proxy_timeout))
    parts.extend([
        'if [ -f "$td/current-plan.md" ]; then',
        f'  $OPCODE_BIN run {opencode_flags} "Read the plan at $td/current-plan.md and execute it fully. Save the implementation diff to $td/implementation-diff.patch. Update $td/agent-status.md as you complete each step. Do not commit, do not push, do not create branches."',
        "  RC=$?",
        "else",
        '  echo "Error: current-plan.md not found in $td"',
        "  RC=1",
        "fi",
        'git diff --no-color > "$td/implementation-diff.patch" 2>/dev/null',
    ])
    parts.extend(
        [
            "if [ $RC -eq 0 ]; then",
            '  echo "Status: needs-review" > "$td/agent-status.md"',
            "else",
            '  echo "Status: failed" > "$td/agent-status.md"',
            "fi",
        ]
    )
    parts.append(
        f'cat > "$td/agent-report.md" << REOF\n'
        f"# Agent Runner Result — {task_id}\n\n"
        f"- Agent: opencode\n"
        f"- Status: $(head -1 \"$td/agent-status.md\" | cut -d' ' -f2)\n"
        f"- Exit code: $RC\n"
        f"- Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"REOF"
    )
    parts.append("exit $RC")
    return "\n".join(parts)


def project_run_agent(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
    router: Any | None = None,
    run_script: Callable[[str, str], dict[str, Any]] | None = None,
    run_script_async: Callable[[str, str], dict[str, Any]] | None = None,
    async_submit: bool = False,
) -> dict[str, Any]:
    """Execute a handoff task via the agent backend router.

    Reads ``task.json`` from the SSH target, validates the task contract
    (``agent``, ``allowed_backends``), selects the backend via the router
    (when enabled), and delegates to the appropriate execution path.

    Args:
        run_cmd: callable(project, command) that executes a shell command
        project: project name under ``MCP_GATEWAY_PROJECT_ROOT``
        task_id: validated ``.ai-bridge`` task ID
        model: optional model override
        router: optional ``AgentBackendRouter`` instance
        run_script: callable(project, script) for multi-line bash scripts
        run_script_async: callable(project, script) -> {"job_id": ...},
            submits without waiting -- required when async_submit=True.
            Fleet mode: call run_agent repeatedly with async_submit=True to
            launch several agents without blocking on each one, then poll
            each job_id independently via job_status/job_result/job_wait.
            The router's cooldown tracking (record_result) is NOT fed by
            async-submitted jobs -- there is no completion callback into
            this process once a job is handed off, so an async run's
            eventual failure/rate-limit never reaches the router. Only the
            synchronous path (async_submit=False) updates cooldown state.
        async_submit: submit and return a job_id immediately instead of
            waiting for the full run.

    Returns:
        dict with keys: task_id, status, exit_code, stdout, stderr,
        started_at, finished_at (async: status="running", job_id set,
        exit_code/stdout/stderr/finished_at are None/empty until polled)
    """
    from examples.mcp_server.agent_tasks import validate_task_id

    validate_task_id(task_id)

    started_at = _now_iso()
    td = f"{TASKS_REL_DIR}/{task_id}"

    task_json = _read_task_json(run_cmd, project, task_id)
    if not task_json:
        return {
            "task_id": task_id,
            "status": "error",
            "error": "task.json not found or empty",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    agent = task_json.get("agent", "auto")
    allowed = task_json.get("allowed_backends", [])
    if agent != "auto":
        allowed = allowed or [agent]
    if not allowed:
        return {
            "task_id": task_id,
            "status": "error",
            "error": "task.json missing allowed_backends",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    if router is not None and getattr(router, "enabled", False):
        preferred = agent if agent == "opencode" else None
        selected = router.select_backend(task_agent=preferred)
    else:
        # Router disabled: use task.json agent field if valid, else first allowed
        selected = agent if agent in allowed else allowed[0]

    if not selected:
        cooldowns = router.get_cooldowns() if router else []
        cooldown_info = "; ".join(f"{c.backend}: blocked until {c.until}" for c in cooldowns)
        return {
            "task_id": task_id,
            "status": "blocked",
            "error": f"all backends unavailable ({cooldown_info})"
            if cooldown_info
            else "no backend available",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    if selected not in allowed:
        return {
            "task_id": task_id,
            "status": "error",
            "error": f"selected backend '{selected}' not in allowed_backends {allowed}",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    if selected == "opencode":
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
        cmd = _build_opencode_script(td, task_id, model)
    else:
        return {
            "task_id": task_id,
            "status": "error",
            "error": f"unsupported backend: {selected}",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

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
        submitted = run_script_async(project, cmd)
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

    if router is not None and selected:
        router.record_result(
            selected,
            exit_code=exit_code if exit_code is not None else -1,
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
        )

    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    try:
        from app.workspace.registry import get_registry
        from examples.mcp_server.mcp_client_tools import _redact_project_root

        project_root = str(get_registry().project_info(project)["root"])
        stdout = _redact_project_root(stdout, project_root)
        stderr = _redact_project_root(stderr, project_root)
    except Exception:
        pass  # redaction failure must not hide a real result

    return {
        "task_id": task_id,
        "status": "needs-review"
        if exit_code == 0
        else "failed"
        if exit_code is not None
        else "error",
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
