"""Read-only Git metadata write-capability probing on the SSH execution plane.

The project root's ordinary filesystem writeability is not evidence that Git can
create index locks, loose objects, branch refs, or HEAD locks.  Registered
projects can be mounted with mixed ownership where some of those components are
writeable and others are not.  This module probes the same SSH-side filesystem
namespace used by project Git mutation tools without creating lock files, refs,
objects, or any other repository state.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

_COMPONENTS = ("index", "objects", "refs", "head")
_GIT_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# The wrapper used by GatewayClient.execute_project_script() creates and removes
# its own temporary script under /tmp.  The body below is repository-read-only:
# git rev-parse plus POSIX test/dirname/printf only.  In particular it never
# creates a test lock/ref/object to infer writeability.
_GIT_WRITE_CAPABILITY_SCRIPT = r"""
set -eu

nearest_writeable_dir() {
    candidate="$1"
    while [ ! -d "$candidate" ]; do
        parent=$(dirname "$candidate")
        if [ "$parent" = "$candidate" ]; then
            return 1
        fi
        candidate="$parent"
    done
    [ -w "$candidate" ] && [ -x "$candidate" ]
}

lock_parent_writeable() {
    target="$1"
    parent=$(dirname "$target")
    nearest_writeable_dir "$parent"
}

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    printf 'available=0\nreason=not_git_repo\n'
    exit 0
fi

index_path=$(git rev-parse --git-path index)
objects_path=$(git rev-parse --git-path objects)
head_path=$(git rev-parse --git-path HEAD)
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || printf 'HEAD')

if [ -n "$target_ref" ]; then
    refs_path=$(git rev-parse --git-path "$target_ref")
elif [ "$branch" != "HEAD" ]; then
    refs_path=$(git rev-parse --git-path "refs/heads/$branch")
else
    refs_path=$(git rev-parse --git-path refs/heads)
fi

index_ok=0
objects_ok=0
refs_ok=0
head_ok=0

if lock_parent_writeable "$index_path"; then
    index_ok=1
fi

if [ -d "$objects_path" ] && [ -w "$objects_path" ] && [ -x "$objects_path" ]; then
    objects_ok=1
    # A loose object may hash into any already-existing fanout directory.  If
    # one of those directories is non-writeable, object creation is not a
    # reliable capability and the probe must fail closed.
    for fanout in "$objects_path"/[0-9a-f][0-9a-f]; do
        if [ ! -e "$fanout" ]; then
            continue
        fi
        if [ ! -d "$fanout" ] || [ ! -w "$fanout" ] || [ ! -x "$fanout" ]; then
            objects_ok=0
            break
        fi
    done
fi

if nearest_writeable_dir "$refs_path"; then
    refs_ok=1
fi

if lock_parent_writeable "$head_path"; then
    head_ok=1
fi

if [ "$branch" = "HEAD" ]; then
    detached=1
else
    detached=0
fi

printf 'available=1\n'
printf 'index=%s\n' "$index_ok"
printf 'objects=%s\n' "$objects_ok"
printf 'refs=%s\n' "$refs_ok"
printf 'head=%s\n' "$head_ok"
printf 'detached=%s\n' "$detached"
""".strip()


def _build_probe_script(branch: str | None = None) -> str:
    """Bind an optional exact branch ref without exposing a shell-injection surface."""
    target_ref = ""
    if branch is not None:
        if not branch or not _GIT_BRANCH_RE.fullmatch(branch) or ".." in branch.split("/"):
            raise ValueError(f"INVALID_INPUT: branch {branch!r} is not valid for Git capability probing")
        target_ref = f"refs/heads/{branch}"
    assignment = f"target_ref={shlex.quote(target_ref)}"
    return _GIT_WRITE_CAPABILITY_SCRIPT.replace("set -eu", f"set -eu\n{assignment}", 1)


def _parse_probe_stdout(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def probe_git_write_capabilities(
    client: Any,
    project: str,
    *,
    branch: str | None = None,
) -> dict[str, Any]:
    """Probe Git metadata write capabilities without mutating the repository.

    The probe runs through ``execute_project_script`` so permission checks use
    the same SSH user and filesystem namespace as project-level Git mutations.
    When ``branch`` is supplied, the refs capability is evaluated for that exact
    ``refs/heads/<branch>`` parent path; otherwise an attached worktree probes
    its current branch ref. Returned data is intentionally host-path-free.
    """
    script = _build_probe_script(branch)
    try:
        raw = client.execute_project_script(
            project,
            script,
            timeout_s=30,
        )
    except Exception as exc:
        return {
            "available": False,
            "reason": "probe_transport_failed",
            "retryable": True,
            "diagnostic": type(exc).__name__,
            "non_mutating": True,
            "execution_plane": "ssh_project",
        }

    if raw.get("exit_code", 1) != 0:
        return {
            "available": False,
            "reason": "probe_command_failed",
            "retryable": True,
            "exit_code": raw.get("exit_code"),
            "non_mutating": True,
            "execution_plane": "ssh_project",
        }

    parsed = _parse_probe_stdout(str(raw.get("stdout", "")))
    if parsed.get("available") != "1":
        return {
            "available": False,
            "reason": parsed.get("reason", "probe_unavailable"),
            "retryable": False,
            "non_mutating": True,
            "execution_plane": "ssh_project",
        }

    if any(parsed.get(name) not in {"0", "1"} for name in (*_COMPONENTS, "detached")):
        return {
            "available": False,
            "reason": "malformed_probe_output",
            "retryable": True,
            "non_mutating": True,
            "execution_plane": "ssh_project",
        }

    components = {
        name: {"writeable": parsed[name] == "1"}
        for name in _COMPONENTS
    }
    blocked = [name for name in _COMPONENTS if not components[name]["writeable"]]
    return {
        "available": True,
        "components": components,
        "blocked_components": blocked,
        "fully_writeable": not blocked,
        "detached": parsed["detached"] == "1",
        "non_mutating": True,
        "execution_plane": "ssh_project",
        "note": (
            "Capability probe is an immediate read-only preflight, not an atomic lease; "
            "mutation tools re-run it immediately before Git and still classify runtime permission failures."
        ),
    }


def probe_script_for_tests(branch: str | None = None) -> str:
    """Expose the bound script for regression assertions without duplicating it."""
    return _build_probe_script(branch)
