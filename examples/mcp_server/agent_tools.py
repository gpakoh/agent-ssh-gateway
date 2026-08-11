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
from urllib.parse import urlsplit

from command_policy import CommandPolicyError  # noqa: F401 -- re-exported for tests

TASKS_REL_DIR = ".ai-bridge/tasks"

# Same markers as quart-platform/opencode-adapter's LIMIT_MARKERS (minus
# the bare "pay", which would false-positive on ordinary opencode output).
# Used to detect a rate-limited proxy in the captured run log and report
# it back to the provider (POST {provider}/proxy/report) for cooldown.
PROXY_LIMIT_MARKERS = (
    "rate limit",
    "too many requests",
    "quota",
    "credits",
    "payment required",
    "free tier",
    "usage limit",
    "please upgrade",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"


def _resolve_project_root(project: str) -> str | None:
    """Resolve a project name to its absolute host root, or None if it
    can't be resolved (unknown project, registry unavailable, etc.) --
    callers must treat a None result as "skip this, don't fail the whole
    call over it" (used for both the script's own `cd` and for redacting
    the root out of returned stdout/stderr)."""
    try:
        from app.workspace.registry import get_registry

        return str(get_registry().project_info(project)["root"])
    except Exception:
        return None


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
    try:
        return json.loads(raw)
    except ValueError:
        # Corrupt/unrelated stdout must not crash the caller. run_agent
        # still fails closed (empty dict -> "task.json not found or empty");
        # run_opencode treats task.json as optional metadata (worktree_path)
        # and proceeds with the plan as its only contract.
        return {}


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


def _proxy_report_script_lines(provider_url: str, timeout: str) -> list[str]:
    """Bash lines that give a rate-limited proxy back to the provider.

    Mirrors quart-platform/opencode-adapter's ProxySource.report_limit:
    when the captured opencode log (written by _build_opencode_script to
    ``$td/opencode-output.log``) matches a rate-limit marker, POST
    ``{provider_base}/proxy/report`` with the leased proxy and a
    best-effort retry_after_seconds parsed from the log text. The provider
    puts the proxy on cooldown (it is NOT deleted from the pool), exactly
    like the adapter's report loop: lease a live proxy before the run,
    return it when the IP hit limits.

    The whole block is best-effort: a failed report must never fail the
    agent run (opencode already finished at this point). Sets RATE_LIMITED=1
    so _build_opencode_script can mark the task status accordingly.
    """
    base = urlsplit(provider_url)
    report_url = f"{base.scheme}://{base.netloc}/proxy/report"
    marker_pattern = "|".join(PROXY_LIMIT_MARKERS)
    return [
        "",
        "# Give a rate-limited proxy back to the provider (POST /proxy/report)",
        'if [ -n "$OPENCODE_PROXY_URL" ] && [ -f "$td/opencode-output.log" ]; then',
        f"  if grep -Eqi {_shell_escape(marker_pattern)} \"$td/opencode-output.log\"; then",
        "    RATE_LIMITED=1",
        '    RETRY_AFTER=$(python3 - "$td/opencode-output.log" <<\'PROXYRETRY_EOF\'',
        "import re, sys",
        "",
        "text = open(sys.argv[1], encoding='utf-8', errors='replace').read()",
        "m = re.search(r'(?:retry(?:ing)?|try\\s+again)\\s+in\\s+(\\d+)\\s*(h(?:our)?s?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)(?:\\s+(\\d+)\\s*(h(?:our)?s?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?))?', text, re.I)",
        "if m:",
        "    total = 0",
        "    for i in range(1, len(m.groups()), 2):",
        "        number, unit = m.group(i), m.group(i + 1)",
        "        if not number or not unit:",
        "            continue",
        "        unit = unit.lower()",
        "        if unit.startswith('h'):",
        "            total += int(number) * 3600",
        "        elif unit.startswith('m'):",
        "            total += int(number) * 60",
        "        else:",
        "            total += int(number)",
        "    if total:",
        "        print(total)",
        "        sys.exit(0)",
        "m2 = re.search(r'\\bretry\\s+after\\s+(\\d+)\\b', text, re.I)",
        "if m2:",
        "    print(int(m2.group(1)))",
        "PROXYRETRY_EOF",
        ")",
        '    [ -n "$RETRY_AFTER" ] || RETRY_AFTER=300',
        '    python3 - "$OPENCODE_PROXY_URL" "$RETRY_AFTER" <<\'PROXYREPORT_EOF\'',
        "import json, sys, urllib.request",
        "",
        "proxy_url, retry_after = sys.argv[1], sys.argv[2]",
        f"report_url = {report_url!r}",
        "try:",
        "    req = urllib.request.Request(",
        "        report_url,",
        "        data=json.dumps({'proxy': proxy_url, 'retry_after_seconds': int(retry_after)}).encode(),",
        "        headers={'Content-Type': 'application/json'},",
        "    )",
        f"    urllib.request.urlopen(req, timeout={timeout})",
        "except Exception:",
        "    pass",
        "PROXYREPORT_EOF",
        '    echo "Rate limited via $OPENCODE_PROXY_URL — reported to provider (retry after ${RETRY_AFTER}s)" >> "$td/agent-status.md"',
        "  fi",
        "fi",
        "",
    ]


def _build_opencode_script(
    td: str,
    task_id: str,
    model: str | None,
    project_root: str | None = None,
    worktree_path: str | None = None,
) -> str:
    opencode_flags = "--dangerously-skip-permissions"
    if model:
        opencode_flags += f" --model {_shell_escape(model)}"

    proxy_provider_url = os.environ.get("OPENCODE_PROXY_PROVIDER_URL", "").strip()
    proxy_timeout = os.environ.get("OPENCODE_PROXY_PROVIDER_TIMEOUT", "5").strip() or "5"

    parts = []
    if project_root:
        # `td` (below) is relative to the project root -- execute_argv's
        # sync dispatch path sets cwd server-side, so this was previously
        # a no-op there, but execute_raw's async dispatch path (used by
        # run_script_async / async_submit=True) has no cwd concept at all:
        # the script would otherwise run from the SSH session's own
        # default directory (its home dir, not the project), and every
        # relative $td reference below would silently resolve to the
        # wrong place -- confirmed live via a real MCP run_agent call,
        # which failed with "current-plan.md not found" despite the file
        # genuinely existing at the right path. Make the script
        # self-sufficient regardless of which dispatch path invoked it.
        parts.append(f"cd {_shell_escape(project_root)} || exit 1")
        # Absolute task dir: a git worktree below is a fresh checkout from
        # HEAD and never contains .ai-bridge (it is gitignored), so a
        # relative $td would resolve inside the worktree to nothing. The
        # plan/status/diff must stay in the main checkout's task dir.
        parts.append(f"td={_shell_escape(os.path.join(project_root, td))}")
    else:
        parts.append(f"td='{td}'")
    parts.extend([
        'mkdir -p "$td"',
        'echo "Status: running" > "$td/agent-status.md"',
        "OPCODE_BIN=$(command -v opencode 2>/dev/null || echo '/root/.opencode/bin/opencode')",
    ])
    if worktree_path:
        wt = worktree_path
        if project_root and not os.path.isabs(wt):
            wt = os.path.normpath(os.path.join(project_root, wt))
        parts.extend([
            f"wt={_shell_escape(wt)}",
            'mkdir -p "$(dirname "$wt")"',
            'if [ -e "$wt/.git" ] || git -C "$wt" rev-parse --git-dir >/dev/null 2>&1; then',
            '  echo "Worktree already exists, reusing: $wt" >> "$td/agent-status.md"',
            "else",
            '  git worktree add --detach "$wt" HEAD 2>>"$td/agent-status.md" || { echo "git worktree add failed: $wt" >> "$td/agent-status.md"; exit 1; }',
            "fi",
            'cd "$wt" || exit 1',
        ])
    if proxy_provider_url:
        parts.extend(_proxy_fetch_script_lines(proxy_provider_url, proxy_timeout))
    parts.extend([
        'if [ -f "$td/current-plan.md" ]; then',
        f'  $OPCODE_BIN run {opencode_flags} "Read the plan at $td/current-plan.md and execute it fully. Save the implementation diff to $td/implementation-diff.patch. Update $td/agent-status.md as you complete each step. Do not commit, do not push, do not create branches." > "$td/opencode-output.log" 2>&1',
        "  RC=$?",
        '  cat "$td/opencode-output.log"',
        "else",
        '  echo "Error: current-plan.md not found in $td"',
        "  RC=1",
        "fi",
    ])
    if proxy_provider_url:
        parts.extend(_proxy_report_script_lines(proxy_provider_url, proxy_timeout))
    parts.extend([
        'git diff --no-color > "$td/implementation-diff.patch" 2>/dev/null',
    ])
    parts.extend(
        [
            "if [ $RC -eq 0 ]; then",
            '  echo "Status: needs-review" > "$td/agent-status.md"',
            'elif [ "${RATE_LIMITED:-0}" = "1" ]; then',
            '  echo "Status: rate-limited" > "$td/agent-status.md"',
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
        project_root = _resolve_project_root(project)
        worktree_path = (task_json.get("worktree_path") or "").strip() or None
        cmd = _build_opencode_script(
            td, task_id, model, project_root=project_root, worktree_path=worktree_path
        )
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
        else "failed"
        if exit_code is not None
        else "error",
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
