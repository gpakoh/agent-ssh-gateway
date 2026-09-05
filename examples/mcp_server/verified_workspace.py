"""Trusted verification contract for externally prepared delivery workspaces."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from examples.mcp_server.agent_tasks import validate_scope_contract
from examples.mcp_server.task_candidate import _compile_scope_glob

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class VerifiedWorkspaceError(RuntimeError):
    """Sanitized verified-workspace failure safe to expose through MCP."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details


def _fail(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> VerifiedWorkspaceError:
    return VerifiedWorkspaceError(
        code,
        message,
        retryable=retryable,
        details=details,
    )


def _sha(value: str, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA1_RE.fullmatch(normalized):
        raise _fail("INVALID_INPUT", f"{label} must be a full lowercase Git SHA-1")
    return normalized


def _git(root: Path, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        return subprocess.run(
            ["git", "-c", f"safe.directory={root}", *args],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail(
            "WORKSPACE_VERIFICATION_FAILED",
            "registered delivery workspace Git verification did not complete",
            retryable=True,
        ) from exc


def verify_registered_delivery_workspace(
    *,
    project_root: str | Path,
    expected_base_sha: str,
    expected_head_sha: str,
    allowed_files: list[str],
) -> dict[str, Any]:
    """Prove exact immutable commit state for a registered delivery workspace."""
    root = Path(project_root).resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise _fail("INVALID_INPUT", "registered delivery workspace must be a Git worktree")

    base = _sha(expected_base_sha, "expected_base_sha")
    head = _sha(expected_head_sha, "expected_head_sha")
    normalized_allowed = [str(item).strip() for item in allowed_files]
    if not normalized_allowed or any(not item for item in normalized_allowed):
        raise _fail("INVALID_INPUT", "allowed_files must contain at least one non-empty pattern")
    try:
        validate_scope_contract(normalized_allowed, [])
        allowed = [(pattern, _compile_scope_glob(pattern)) for pattern in normalized_allowed]
    except Exception as exc:
        raise _fail("INVALID_INPUT", "allowed_files contains an invalid scope pattern") from exc

    status = _git(root, ["status", "--porcelain=v1", "-z"])
    if status.returncode != 0:
        raise _fail("WORKSPACE_VERIFICATION_FAILED", "could not inspect delivery workspace status")
    if status.stdout:
        raise _fail(
            "WORKSPACE_DIRTY",
            "delivery workspace must be clean before trusted push",
            details={
                "status_sha256": hashlib.sha256(status.stdout).hexdigest(),
                "status_bytes": len(status.stdout),
            },
        )

    resolved_head = _git(root, ["rev-parse", "HEAD"])
    if resolved_head.returncode != 0 or resolved_head.stdout.decode().strip().lower() != head:
        raise _fail("HEAD_MISMATCH", "delivery workspace HEAD does not match expected_head_sha")

    for label, sha in (("base", base), ("head", head)):
        exists = _git(root, ["cat-file", "-e", f"{sha}^{{commit}}"])
        if exists.returncode != 0:
            raise _fail(
                "SOURCE_REF_NOT_AVAILABLE",
                f"expected {label} commit is unavailable in delivery workspace",
                retryable=True,
                details={f"expected_{label}_sha": sha},
            )

    ancestor = _git(root, ["merge-base", "--is-ancestor", base, head])
    if ancestor.returncode != 0:
        raise _fail(
            "BASE_MISMATCH",
            "expected_base_sha is not an ancestor of expected_head_sha",
            details={"expected_base_sha": base, "expected_head_sha": head},
        )

    changed = _git(
        root,
        ["diff", "--no-renames", "--name-only", "-z", base, head, "--"],
    )
    if changed.returncode != 0:
        raise _fail("WORKSPACE_VERIFICATION_FAILED", "could not inspect delivery commit scope")
    changed_files = [
        part.decode("utf-8", "surrogateescape")
        for part in changed.stdout.split(b"\0")
        if part
    ]
    if not changed_files:
        raise _fail("CHECK_FAILED", "delivery commit contains no changes from expected_base_sha")
    for path in changed_files:
        if not any(regex.fullmatch(path) for _pattern, regex in allowed):
            raise _fail(
                "CANDIDATE_SCOPE_VIOLATION",
                "delivery commit changes a file outside allowed_files",
                details={"path": path},
            )

    return {
        "base_sha": base,
        "head_sha": head,
        "clean": True,
        "status_sha256": hashlib.sha256(status.stdout).hexdigest(),
        "changed_files": sorted(changed_files),
        "allowed_files": normalized_allowed,
        "scope_verified": True,
    }


__all__ = [
    "VerifiedWorkspaceError",
    "verify_registered_delivery_workspace",
]
