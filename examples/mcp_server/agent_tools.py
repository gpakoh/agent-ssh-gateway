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

from examples.mcp_server.agent_paths import task_dir

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

    cmd = f"cat {shlex.quote(f'{task_dir(project, task_id)}/task.json')}"
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


def _task_string_list(task_json: dict[str, Any], key: str) -> list[str]:
    """Return a validated list[str] from the supervisor task contract."""
    value = task_json.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"task.json field {key!r} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _read_current_plan(
    run_cmd: Callable[[str, str], dict[str, Any]],
    project: str,
    task_id: str,
) -> str | None:
    import shlex

    td = task_dir(project, task_id)
    cmd = f"cat {shlex.quote(f'{td}/current-plan.md')}"
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


def _parent_prerun_snapshot_script_lines(project_root: str) -> list[str]:
    """Capture the parent checkout's complete non-ignored working-tree state.

    A temporary index produces a tree object without touching the real Git
    index.  The post-run supervisor recomputes the same tree and rejects any
    worker that reached back out of its worktree and mutated the parent.
    """
    return [
        f"PARENT_ROOT={_shell_escape(project_root)}",
        'PARENT_HEAD_BEFORE=$(git -C "$PARENT_ROOT" rev-parse HEAD 2>/dev/null) || { echo "Parent HEAD snapshot FAILED" >> "$td/agent-status.md"; exit 75; }',
        'PARENT_INDEX_TREE_BEFORE=$(git -C "$PARENT_ROOT" write-tree 2>/dev/null) || { echo "Parent index snapshot FAILED" >> "$td/agent-status.md"; exit 75; }',
        'printf "%s\\n" "$PARENT_HEAD_BEFORE" > "$td/parent-head-before.txt"',
        'printf "%s\\n" "$PARENT_INDEX_TREE_BEFORE" > "$td/parent-index-tree-before.txt"',
        'PARENT_INDEX="$td/.parent-index-before"',
        'rm -f "$PARENT_INDEX" "$PARENT_INDEX.lock"',
        'if GIT_INDEX_FILE="$PARENT_INDEX" git -C "$PARENT_ROOT" read-tree HEAD >/dev/null 2>&1 && \\',
        '   GIT_INDEX_FILE="$PARENT_INDEX" git -C "$PARENT_ROOT" add -A -- . >/dev/null 2>&1; then',
        '  PARENT_TREE_BEFORE=$(GIT_INDEX_FILE="$PARENT_INDEX" git -C "$PARENT_ROOT" write-tree 2>/dev/null)',
        '  [ -n "$PARENT_TREE_BEFORE" ] || { echo "Parent snapshot capture FAILED" >> "$td/agent-status.md"; rm -f "$PARENT_INDEX" "$PARENT_INDEX.lock"; exit 75; }',
        '  printf "%s\\n" "$PARENT_TREE_BEFORE" > "$td/parent-tree-before.txt"',
        "else",
        '  echo "Parent snapshot capture FAILED" >> "$td/agent-status.md"',
        '  rm -f "$PARENT_INDEX" "$PARENT_INDEX.lock"',
        "  exit 75",
        "fi",
        'rm -f "$PARENT_INDEX" "$PARENT_INDEX.lock"',
    ]


def _isolated_worktree_error(project_root: str | None, worktree_path: str | None) -> str | None:
    """Return an error when a real project would execute an agent in parent.

    Registry-less unit-test/fallback contexts retain the historical behavior;
    production projects always resolve a root and therefore require an
    explicit worktree distinct from the parent checkout.
    """
    if not project_root:
        return None
    if not worktree_path or not worktree_path.strip():
        return "isolated worktree_path is required for agent execution"
    raw = worktree_path.strip()
    resolved = raw if os.path.isabs(raw) else os.path.join(project_root, raw)
    if os.path.realpath(resolved) == os.path.realpath(project_root):
        return "agent execution in the parent checkout is forbidden; use an isolated worktree"
    return None


def _supervisor_postrun_script_lines(
    allowed_files: list[str],
    forbidden_files: list[str],
    required_checks: list[str],
    parent_root: str | None = None,
) -> list[str]:
    """Build fail-closed post-run evidence, scope and check enforcement.

    The contract values are captured by the MCP process before OpenCode starts
    and embedded into the runner script.  The worker therefore cannot relax
    its own allowed/forbidden scope by rewriting task.json while it runs.

    Evidence is generated with a temporary Git index rooted at BASE_HEAD. This
    includes tracked edits, deletions, staged changes and previously-untracked
    files without mutating the worktree's real index.  BASE_HEAD is captured
    before the worker starts, so an illicit worker commit cannot make its
    changes disappear from the supervisor diff.
    """
    allowed_json = json.dumps(allowed_files, separators=(",", ":"))
    forbidden_json = json.dumps(forbidden_files, separators=(",", ":"))
    parent_post_lines: list[str] = []
    if parent_root:
        parent_post_lines = [
            f"PARENT_ROOT={_shell_escape(parent_root)}",
            'PARENT_HEAD_AFTER=$(git -C "$PARENT_ROOT" rev-parse HEAD 2>/dev/null || true)',
            'PARENT_INDEX_TREE_AFTER=$(git -C "$PARENT_ROOT" write-tree 2>/dev/null || true)',
            'printf "%s\\n" "$PARENT_HEAD_AFTER" > "$td/parent-head-after.txt"',
            'printf "%s\\n" "$PARENT_INDEX_TREE_AFTER" > "$td/parent-index-tree-after.txt"',
            'if [ -z "$PARENT_HEAD_AFTER" ] || [ "$PARENT_HEAD_AFTER" != "$PARENT_HEAD_BEFORE" ] || [ -z "$PARENT_INDEX_TREE_AFTER" ] || [ "$PARENT_INDEX_TREE_AFTER" != "$PARENT_INDEX_TREE_BEFORE" ]; then',
            "  PARENT_RC=1",
            "fi",
            'PARENT_INDEX="$td/.parent-index-after"',
            'rm -f "$PARENT_INDEX" "$PARENT_INDEX.lock"',
            'if GIT_INDEX_FILE="$PARENT_INDEX" git -C "$PARENT_ROOT" read-tree HEAD >/dev/null 2>&1 && \\',
            '   GIT_INDEX_FILE="$PARENT_INDEX" git -C "$PARENT_ROOT" add -A -- . >/dev/null 2>&1; then',
            '  PARENT_TREE_AFTER=$(GIT_INDEX_FILE="$PARENT_INDEX" git -C "$PARENT_ROOT" write-tree 2>/dev/null)',
            "else",
            "  PARENT_TREE_AFTER=",
            "  PARENT_RC=1",
            "fi",
            'rm -f "$PARENT_INDEX" "$PARENT_INDEX.lock"',
            'if [ -z "$PARENT_TREE_AFTER" ] || [ "$PARENT_TREE_AFTER" != "$PARENT_TREE_BEFORE" ]; then',
            "  PARENT_RC=1",
            '  echo "Supervisor parent-checkout guard FAILED" >> "$td/agent-status.md"',
            "else",
            '  echo "Supervisor parent-checkout guard passed" >> "$td/agent-status.md"',
            "fi",
            'printf "%s\\n" "$PARENT_TREE_AFTER" > "$td/parent-tree-after.txt"',
        ]

    lines = [
        "",
        "# Supervisor-owned evidence collection and contract enforcement",
        "PARENT_RC=0",
        "EVIDENCE_RC=0",
        "SCOPE_RC=0",
        "CHECKS_RC=0",
        "SCOPE_RAN=0",
        "CHECKS_RAN=0",
        'SUPERVISOR_INDEX="$td/.supervisor-index"',
        'rm -f "$SUPERVISOR_INDEX" "$td/changed-files.z" "$td/scope-violations.json"',
        'POST_HEAD=$(git rev-parse HEAD 2>/dev/null || true)',
        'if GIT_INDEX_FILE="$SUPERVISOR_INDEX" git read-tree "$BASE_HEAD" >/dev/null 2>&1 && \\',
        '   GIT_INDEX_FILE="$SUPERVISOR_INDEX" git add -A -- . >/dev/null 2>&1 && \\',
        '   GIT_INDEX_FILE="$SUPERVISOR_INDEX" git diff --cached --binary --no-color "$BASE_HEAD" -- > "$td/implementation-diff.patch" 2>/dev/null && \\',
        '   GIT_INDEX_FILE="$SUPERVISOR_INDEX" git diff --cached --name-only -z "$BASE_HEAD" -- > "$td/changed-files.z" 2>/dev/null; then',
        '  echo "Supervisor evidence collected" >> "$td/agent-status.md"',
        "else",
        "  EVIDENCE_RC=1",
        '  : > "$td/implementation-diff.patch"',
        '  echo "Supervisor evidence collection FAILED" >> "$td/agent-status.md"',
        "fi",
        'rm -f "$SUPERVISOR_INDEX"',
        'if [ "$EVIDENCE_RC" -eq 0 ]; then',
        "  SCOPE_RAN=1",
        f"  python3 - \"$td/changed-files.z\" {_shell_escape(allowed_json)} {_shell_escape(forbidden_json)} \"$BASE_HEAD\" \"$POST_HEAD\" \"$td/scope-violations.json\" <<'SCOPE_EOF'",
        "import json, re, sys",
        "from pathlib import PurePosixPath",
        "",
        "changed_path, allowed_raw, forbidden_raw, base_head, post_head, report_path = sys.argv[1:]",
        "allowed = json.loads(allowed_raw)",
        "forbidden = json.loads(forbidden_raw)",
        "raw = open(changed_path, 'rb').read()",
        "changed = [p.decode('utf-8', 'surrogateescape') for p in raw.split(b'\\0') if p]",
        "",
        "def compile_glob(pattern):",
        "    pattern = pattern.replace('\\\\', '/').strip()",
        "    while pattern.startswith('./'):",
        "        pattern = pattern[2:]",
        "    parts = PurePosixPath(pattern).parts if pattern else ()",
        "    if not pattern or pattern.startswith('/') or '..' in parts:",
        "        raise ValueError(f'invalid scope pattern: {pattern!r}')",
        "    out = ['^']",
        "    i = 0",
        "    while i < len(pattern):",
        "        if pattern[i:i+3] == '**/':",
        "            out.append('(?:.*/)?')",
        "            i += 3",
        "        elif pattern[i:i+2] == '**':",
        "            out.append('.*')",
        "            i += 2",
        "        elif pattern[i] == '*':",
        "            out.append('[^/]*')",
        "            i += 1",
        "        elif pattern[i] == '?':",
        "            out.append('[^/]')",
        "            i += 1",
        "        else:",
        "            out.append(re.escape(pattern[i]))",
        "            i += 1",
        "    out.append('$')",
        "    return re.compile(''.join(out))",
        "",
        "violations = []",
        "try:",
        "    allowed_rx = [(p, compile_glob(p)) for p in allowed]",
        "    forbidden_rx = [(p, compile_glob(p)) for p in forbidden]",
        "except (TypeError, ValueError) as exc:",
        "    violations.append({'type': 'invalid-contract', 'detail': str(exc)})",
        "    allowed_rx = []",
        "    forbidden_rx = []",
        "",
        "for path in changed:",
        "    if not any(rx.fullmatch(path) for _, rx in allowed_rx):",
        "        violations.append({'type': 'outside-allowed-files', 'path': path})",
        "    for pattern, rx in forbidden_rx:",
        "        if rx.fullmatch(path):",
        "            violations.append({'type': 'forbidden-file', 'path': path, 'pattern': pattern})",
        "            break",
        "if not post_head or post_head != base_head:",
        "    violations.append({'type': 'head-changed', 'base_head': base_head, 'post_head': post_head})",
        "",
        "report = {",
        "    'base_head': base_head,",
        "    'post_head': post_head,",
        "    'changed_files': changed,",
        "    'allowed_files': allowed,",
        "    'forbidden_files': forbidden,",
        "    'violations': violations,",
        "}",
        "with open(report_path, 'w', encoding='utf-8') as fh:",
        "    json.dump(report, fh, ensure_ascii=False, indent=2)",
        "    fh.write('\\n')",
        "if violations:",
        "    for item in violations:",
        "        print('scope violation:', item, file=sys.stderr)",
        "    sys.exit(3)",
        "SCOPE_EOF",
        "  SCOPE_RC=$?",
        '  if [ "$SCOPE_RC" -eq 0 ]; then',
        '    echo "Supervisor scope check passed" >> "$td/agent-status.md"',
        "  else",
        '    echo "Supervisor scope check FAILED" >> "$td/agent-status.md"',
        "  fi",
        "else",
        '  echo "Supervisor scope check skipped: evidence unavailable" >> "$td/agent-status.md"',
        "fi",
        *parent_post_lines,
        'if [ "$RC" -eq 0 ] && [ "$PARENT_RC" -eq 0 ] && [ "$EVIDENCE_RC" -eq 0 ] && [ "$SCOPE_RC" -eq 0 ]; then',
        "  CHECKS_RAN=1",
        '  : > "$td/required-checks.log"',
    ]

    for check in required_checks:
        lines.extend(
            [
                '  if [ "$CHECKS_RC" -eq 0 ]; then',
                f'    echo {_shell_escape("$ " + check)} >> "$td/required-checks.log"',
                f"    if sh -c {_shell_escape(check)} >> \"$td/required-checks.log\" 2>&1; then",
                '      echo "PASS" >> "$td/required-checks.log"',
                "    else",
                "      CHECKS_RC=$?",
                '      echo "FAIL exit=$CHECKS_RC" >> "$td/required-checks.log"',
                "    fi",
                "  fi",
            ]
        )

    lines.extend(
        [
            "fi",
            'if [ "$CHECKS_RAN" -eq 0 ]; then',
            '  echo "Supervisor required checks skipped" >> "$td/agent-status.md"',
            'elif [ "$CHECKS_RC" -eq 0 ]; then',
            '  echo "Supervisor required checks passed" >> "$td/agent-status.md"',
            "else",
            '  echo "Supervisor required checks FAILED" >> "$td/agent-status.md"',
            "fi",
            # Required checks are supervisor-triggered shell commands and can
            # still reach the parent checkout via absolute paths. Re-run the
            # authoritative parent snapshot after every required check so a
            # persistent parent mutation cannot happen after the first guard
            # and escape detection. PARENT_RC is intentionally not reset: an
            # earlier worker-side contamination remains sticky.
            *parent_post_lines,
            "FINAL_RC=$RC",
            'if [ "$EVIDENCE_RC" -ne 0 ]; then FINAL_RC=70; fi',
            'if [ "$SCOPE_RC" -ne 0 ]; then FINAL_RC=71; fi',
            'if [ "$CHECKS_RC" -ne 0 ]; then FINAL_RC=72; fi',
            'if [ "$PARENT_RC" -ne 0 ]; then FINAL_RC=74; fi',
        ]
    )
    return lines


def _build_opencode_script(
    td: str,
    task_id: str,
    model: str | None,
    project_root: str | None = None,
    worktree_path: str | None = None,
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    required_checks: list[str] | None = None,
) -> str:
    opencode_flags = "--dangerously-skip-permissions"
    if model:
        opencode_flags += f" --model {_shell_escape(model)}"

    proxy_provider_url = os.environ.get("OPENCODE_PROXY_PROVIDER_URL", "").strip()
    proxy_timeout = os.environ.get("OPENCODE_PROXY_PROVIDER_TIMEOUT", "5").strip() or "5"
    allowed_files = list(allowed_files or [])
    forbidden_files = list(forbidden_files or [])
    required_checks = list(required_checks or [])

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
    if project_root and worktree_path:
        parts.extend(_parent_prerun_snapshot_script_lines(project_root))
    if worktree_path:
        wt = worktree_path
        if project_root and not os.path.isabs(wt):
            wt = os.path.normpath(os.path.join(project_root, wt))
        parts.extend([
            f"wt={_shell_escape(wt)}",
            'mkdir -p "$(dirname "$wt")"',
            'if [ -e "$wt" ]; then',
            '  wt_real=$(cd "$wt" 2>/dev/null && pwd -P || true)',
            '  wt_top=$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null || true)',
            '  wt_top_real=$(cd "$wt_top" 2>/dev/null && pwd -P || true)',
            '  if [ -z "$wt_real" ] || [ -z "$wt_top_real" ] || [ "$wt_real" != "$wt_top_real" ]; then',
            '    echo "Refusing non-worktree-root path: $wt" >> "$td/agent-status.md"',
            "    exit 1",
            "  fi",
            '  if [ -n "$(git -C "$wt" status --porcelain=v1 --untracked-files=all)" ]; then',
            '    echo "Refusing dirty existing worktree: $wt" >> "$td/agent-status.md"',
            "    exit 1",
            "  fi",
            '  echo "Worktree already exists, reusing clean root: $wt" >> "$td/agent-status.md"',
            "else",
            '  git worktree add --detach "$wt" HEAD 2>>"$td/agent-status.md" || { echo "git worktree add failed: $wt" >> "$td/agent-status.md"; exit 1; }',
            "fi",
            'cd "$wt" || exit 1',
            # Isolate opencode's storage per run: parallel fleet agents
            # share one HOME (~/.local/share/opencode/opencode.db) and two
            # concurrent processes race SQLite schema migration / WAL
            # locking ("Failed query: CREATE TABLE ..." -- seen live in the
            # E2E smoke). Point XDG data+cache at the task dir (gitignored,
            # OUTSIDE the worktree); config (~/.config/opencode) is
            # read-only and safe to share.
            # Storing under $wt is fatal: opencode's background snapshot
            # `git add --all --sparse` uses the worktree as --work-tree and
            # would then add .opencode-data -- including the snapshot repo's
            # own object store -- re-scanning its own growth forever; the
            # opencode process waits for the snapshot and never exits, so
            # the runner script hangs (job timeout 600s, seen live).
            f'export XDG_DATA_HOME="{td}/.opencode-data"',
            f'export XDG_CACHE_HOME="{td}/.opencode-cache"',
            'mkdir -p "$XDG_DATA_HOME" "$XDG_CACHE_HOME"',
        ])
    parts.extend([
        'BASE_HEAD=$(git rev-parse HEAD 2>/dev/null) || { echo "Supervisor baseline capture FAILED" >> "$td/agent-status.md"; exit 73; }',
        'printf "%s\\n" "$BASE_HEAD" > "$td/base-head.txt"',
    ])
    if proxy_provider_url:
        parts.extend(_proxy_fetch_script_lines(proxy_provider_url, proxy_timeout))
    parts.extend([
        'if [ -f "$td/current-plan.md" ]; then',
        # stdin must be /dev/null for opencode: the script itself is piped
        # to `sh` via stdin (execute_project_script*), and a long-running
        # child that reads stdin steals the script tail the shell has not
        # consumed yet -- sh then hits EOF with an unclosed construct
        # ("sh: syntax error: unexpected end of file (expecting \"fi\")",
        # seen live in the E2E smoke) and aborts before the post-run
        # status/report block. Children inherit /dev/null, so this covers
        # the whole opencode subtree.
        f'  $OPCODE_BIN run {opencode_flags} < /dev/null "Read the plan at $td/current-plan.md and execute it fully. Save the implementation diff to $td/implementation-diff.patch. Update $td/agent-status.md as you complete each step. Do not commit, do not push, do not create branches." > "$td/opencode-output.log" 2>&1',
        "  RC=$?",
        '  cat "$td/opencode-output.log"',
        "else",
        '  echo "Error: current-plan.md not found in $td"',
        "  RC=1",
        "fi",
        # Preserve the detailed worker-authored status before supervisor/proxy
        # post-processing replaces agent-status.md with its canonical final
        # one-line state. This survives MCP restarts and keeps the worker's
        # step log/deliverables available for later review.
        'if [ -f "$td/agent-status.md" ]; then cp "$td/agent-status.md" "$td/worker-status.md"; fi',
    ])
    if proxy_provider_url:
        parts.extend(_proxy_report_script_lines(proxy_provider_url, proxy_timeout))
    parts.extend(
        _supervisor_postrun_script_lines(
            allowed_files,
            forbidden_files,
            required_checks,
            parent_root=project_root if worktree_path else None,
        )
    )
    parts.extend(
        [
            "if [ $FINAL_RC -eq 0 ]; then",
            '  echo "Status: needs-review" > "$td/agent-status.md"',
            'elif [ "${RATE_LIMITED:-0}" = "1" ] && [ "$PARENT_RC" -eq 0 ] && [ "$EVIDENCE_RC" -eq 0 ] && [ "$SCOPE_RC" -eq 0 ] && [ "$CHECKS_RC" -eq 0 ]; then',
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
        f"- Worker exit code: $RC\n"
        f"- Final exit code: $FINAL_RC\n"
        f"- Parent-guard exit code: $PARENT_RC\n"
        f"- Evidence exit code: $EVIDENCE_RC\n"
        f"- Scope exit code: $SCOPE_RC (ran=$SCOPE_RAN)\n"
        f"- Required-checks exit code: $CHECKS_RC (ran=$CHECKS_RAN)\n"
        f"- Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"REOF"
    )
    parts.extend([
        'if [ -s "$td/worker-status.md" ]; then',
        '  printf "\\n## Worker status snapshot\\n\\n" >> "$td/agent-report.md"',
        '  cat "$td/worker-status.md" >> "$td/agent-report.md"',
        '  printf "\\n" >> "$td/agent-report.md"',
        "fi",
    ])
    parts.append("exit $FINAL_RC")
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
    td = task_dir(project, task_id)

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
            allowed_files = _task_string_list(task_json, "allowed_files")
            forbidden_files = _task_string_list(task_json, "forbidden_files")
            required_checks = _task_string_list(task_json, "required_checks")
        except ValueError as exc:
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
