"""Bounded, owned process-group runner for verifier uv invocations.

The verifier shell retains FD 9 for the entire synchronous operation. Its uv
child inherits FD 9 too: if the SSH shell itself is terminated, descendants
must *not* silently release the exclusive environment lock while they can
still be writing into its .venv. An in-shell Python supervisor enforces a
shorter deadline than the outer SSH job and signals ONLY the process group
it spawned. A child that escapes that group still holds FD 9 (fail closed).
"""

from __future__ import annotations

import shlex

_VERIFIER_CHILD_SECONDS = 240
_VERIFIER_CHILD_MAX_SECONDS = 3600
VERIFIER_SYNC_COMMAND_TIMEOUT_S = 270
VERIFIER_SYNC_WAIT_TIMEOUT_S = 295
VERIFIER_CHILD_TIMEOUT_EXIT_CODE = 124

# Executed only from the trusted verifier script after 'exec 9>"$LOCK"'.
# No user-provided Python source, target path, argv or token is echoed.
_OWNED_CHILD_SOURCE = """
import math
import os
import signal
import subprocess
import sys
import time

# One absolute deadline is shared by ALL uv commands in this verifier script,
# including fallback sync and requirements install. Per-call budgets would
# otherwise let the outer SSH job expire during a later uv invocation.
try:
    remaining = float(sys.argv[1]) - time.monotonic()
except (ValueError, IndexError):
    remaining = float("nan")
argv = sys.argv[2:]
if not math.isfinite(remaining) or remaining > 3600 or not argv:
    sys.stderr.write("VERIFIER_CHILD_LAUNCH_FAILED: invalid bounded command\\n")
    raise SystemExit(90)
if remaining <= 0:
    sys.stderr.write("VERIFIER_CHILD_TIMEOUT: owned verifier deadline exhausted\\n")
    raise SystemExit(124)

try:
    child = subprocess.Popen(argv, start_new_session=True, pass_fds=(9,))
except OSError:
    sys.stderr.write("VERIFIER_CHILD_LAUNCH_FAILED: execution unavailable\\n")
    raise SystemExit(90)

def signal_owned(sig):
    # The leader remains our unreaped child until the final wait; its PID
    # cannot be reused here. Refuse to signal any other process group.
    try:
        if os.getpgid(child.pid) == child.pid:
            os.killpg(child.pid, sig)
    except ProcessLookupError:
        pass

def stop_owned():
    signal_owned(signal.SIGTERM)
    time.sleep(0.5)
    signal_owned(signal.SIGKILL)
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass

def on_signal(signum, _frame):
    stop_owned()
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)

try:
    code = child.wait(timeout=remaining)
except subprocess.TimeoutExpired:
    stop_owned()
    sys.stderr.write("VERIFIER_CHILD_TIMEOUT: owned verifier process exceeded deadline\\n")
    raise SystemExit(124)

raise SystemExit(code if code >= 0 else 128 - code)
"""


def owned_uv_guard_lines(*, timeout_seconds: int = _VERIFIER_CHILD_SECONDS) -> list[str]:
    """Emit a reusable shell function, keeping an inherited FD 9 fail-closed.

    Every guarded uv invocation preserves the exact original argv and exit
    status. The child uses a fresh process group and a bounded wait so an
    ordinary long-running uv/pytest check cannot consume the full SSH job
    deadline and strand the verifier's lock in its descendants.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= _VERIFIER_CHILD_MAX_SECONDS:
        raise ValueError("verifier child timeout must be an integer from 1 to 3600")
    deadline_source = f"import time; print(time.monotonic() + {timeout_seconds})"
    return [
        # Evaluate once, before acquiring FD 9. This bounds cumulative sync,
        # install and test execution, not just one uv subprocess.
        f'_MCP_VERIFIER_DEADLINE="$(python3 -c {shlex.quote(deadline_source)})"',
        "verifier_uv() {",
        f'  python3 -c {shlex.quote(_OWNED_CHILD_SOURCE)} "$_MCP_VERIFIER_DEADLINE" "$@"',
        "}",
    ]
