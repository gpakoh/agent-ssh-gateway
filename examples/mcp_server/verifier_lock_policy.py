"""Bounded per-project verifier lock acquisition and redacted diagnostics.

Only generates a BusyBox-compatible shell guard. Project identity is hashed
before it enters the script; paths and secrets must never appear in lock
contention messages. Filesystem lifecycle stays with the calling verifier.
"""

from __future__ import annotations

import hashlib

LOCK_BUSY_EXIT_CODE = 91


def lock_scope_id(project_identity: str) -> str:
    """Stable opaque diagnostic key without exposing the workspace path."""
    if not isinstance(project_identity, str) or not project_identity:
        raise ValueError("verifier lock identity must be non-empty")
    return hashlib.sha256(project_identity.encode("utf-8")).hexdigest()[:12]


def lock_acquire_script(project_identity: str, *, wait_seconds: int = 12) -> list[str]:
    """Bound BusyBox flock -n polling, with fixed-shape, path-free evidence."""
    if type(wait_seconds) is not int or not 1 <= wait_seconds <= 60:
        raise ValueError("verifier lock wait must be 1..60 seconds")
    scope = lock_scope_id(project_identity)
    return [
        'if ! command -v flock >/dev/null 2>&1; then',
        '  echo "VERIFIER_BOOTSTRAP_FAILED: flock executable unavailable" >&2',
        '  exit 90',
        'fi',
        # BusyBox flock has -n but no -w. Keep the wait finite without
        # killing an unrelated holder or removing its lock inode.
        '_mcp_verifier_wait=0',
        'while ! flock -n 9 2>/dev/null; do',
        '  if [ "$_mcp_verifier_wait" -eq 0 ]; then',
        f'    echo "VERIFIER_LOCK_WAIT: scope={scope} waited_s=0" >&2',
        '  fi',
        f'  if [ "$_mcp_verifier_wait" -ge {wait_seconds} ]; then',
        f'    echo "VERIFIER_LOCK_BUSY: verifier synchronization is occupied; scope={scope} waited_s=$_mcp_verifier_wait" >&2',
        f'    exit {LOCK_BUSY_EXIT_CODE}',
        '  fi',
        '  sleep 1',
        '  _mcp_verifier_wait=$((_mcp_verifier_wait + 1))',
        'done',
    ]
