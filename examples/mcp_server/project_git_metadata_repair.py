"""Lease-guarded replacement of one registered project's root Git metadata.

This is deliberately *not* a generic rename/delete primitive.  It can only
promote one immediate ``.git.repair-*`` directory to ``.git`` after proving
both the stale canonical metadata and the replacement repository against exact
caller leases.  The displaced metadata is preserved as a deterministic sibling
quarantine outside the worktree so the repair itself does not leave Git status
dirty.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPLACEMENT_RE = re.compile(r"^\.git\.repair-[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_LOCK_FILENAME = ".project-git-metadata-repair.lock"
_LOCK_TIMEOUT_S = 30.0
_LOCK_POLL_S = 0.05
_PROCESS_LOCK = threading.RLock()


class ProjectGitMetadataRepairError(ValueError):
    """Typed fail-closed validation/recovery error for metadata replacement."""

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
        self.details = details or {}


@dataclass(frozen=True)
class ProjectGitMetadataRepairResult:
    branch: str
    previous_head: str
    replacement_head: str
    replacement_dir: str
    quarantine_id: str
    already_repaired: bool
    tracked_worktree_clean: bool
    quarantine_location: str = "sibling"

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "previous_head": self.previous_head,
            "replacement_head": self.replacement_head,
            "replacement_dir": self.replacement_dir,
            "quarantine_id": self.quarantine_id,
            "already_repaired": self.already_repaired,
            "tracked_worktree_clean": self.tracked_worktree_clean,
            "quarantine_location": self.quarantine_location,
        }


def _error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> ProjectGitMetadataRepairError:
    return ProjectGitMetadataRepairError(
        code,
        message,
        retryable=retryable,
        details=details,
    )


def _normalize_sha(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise _error("INVALID_INPUT", f"{field} must be a full Git object id.")
    normalized = value.strip().lower()
    if not _SHA_RE.fullmatch(normalized):
        raise _error("INVALID_INPUT", f"{field} must be a full 40- or 64-character hex Git object id.")
    return normalized


def _normalize_branch(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _BRANCH_RE.fullmatch(value)
        or ".." in value.split("/")
        or value.endswith("/")
    ):
        raise _error("INVALID_INPUT", "expected_branch is not a safe Git branch name.")
    return value


def _normalize_replacement_dir(value: str) -> str:
    if not isinstance(value, str) or not _REPLACEMENT_RE.fullmatch(value):
        raise _error(
            "INVALID_INPUT",
            "replacement_dir must be one immediate .git.repair-* directory name.",
        )
    return value


def _real_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise _error("CHECK_FAILED", f"{label} is missing.", retryable=True) from exc
    except OSError as exc:
        raise _error("TOOL_EXECUTION_FAILED", f"{label} cannot be inspected.") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _error("POLICY_DENIED", f"{label} must be a real directory, not a symlink.")


def _git_output(git_dir: Path, worktree: Path, args: list[str]) -> str | None:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(
            [
                "git",
                f"--git-dir={git_dir}",
                f"--work-tree={worktree}",
                *args,
            ],
            cwd=str(worktree),
            env=env,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _git_identity(git_dir: Path, worktree: Path) -> tuple[str, str] | None:
    head = _git_output(git_dir, worktree, ["rev-parse", "--verify", "HEAD^{commit}"])
    branch = _git_output(git_dir, worktree, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if not head or not branch:
        return None
    head = head.strip().lower()
    if not _SHA_RE.fullmatch(head):
        return None
    return branch.strip(), head


def _tracked_worktree_clean(git_dir: Path, worktree: Path) -> bool:
    status = _git_output(
        git_dir,
        worktree,
        ["status", "--porcelain=v1", "--untracked-files=no"],
    )
    return status == ""


def _quarantine_name(project_root: Path, previous_head: str, replacement_head: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_root.name).strip("._-") or "project"
    root_digest = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:10]
    return (
        f".{slug}.git-quarantine-{root_digest}-"
        f"{previous_head[:12]}-to-{replacement_head[:12]}"
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _repair_lock(journal_root: Path) -> Iterator[None]:
    """Serialize one project's metadata swap using server-owned journal state."""
    if not _PROCESS_LOCK.acquire(timeout=_LOCK_TIMEOUT_S):
        raise _error("WORKSPACE_CONTENDED", "Git metadata repair lock is busy.", retryable=True)
    try:
        try:
            journal_root.mkdir(parents=True, exist_ok=True)
            root_stat = journal_root.lstat()
        except OSError as exc:
            raise _error("TOOL_EXECUTION_FAILED", "Git metadata repair lock root is unavailable.") from exc
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise _error("POLICY_DENIED", "Git metadata repair lock root is unsafe.")

        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise _error("POLICY_DENIED", "Secure Git metadata repair locking is unavailable.")
        lock_path = journal_root / _LOCK_FILENAME
        flags = os.O_RDWR | os.O_CREAT | nofollow | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise _error("TOOL_EXECUTION_FAILED", "Git metadata repair lock cannot be opened.") from exc

        locked = False
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise _error("POLICY_DENIED", "Git metadata repair lock file is unsafe.")
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + _LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise _error(
                            "WORKSPACE_CONTENDED",
                            "Git metadata repair lock is busy.",
                            retryable=True,
                        ) from exc
                    time.sleep(_LOCK_POLL_S)
            yield
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    finally:
        _PROCESS_LOCK.release()


def repair_project_git_metadata(
    *,
    project_root: Path,
    journal_root: Path,
    replacement_dir: str,
    expected_branch: str,
    expected_current_head: str,
    expected_replacement_head: str,
) -> ProjectGitMetadataRepairResult:
    """Promote one exact replacement Git dir while preserving stale metadata.

    The project parent must be writable for the usual sibling quarantine.
    On EXDEV, stale metadata is atomically nested under the promoted Git dir
    on the same filesystem so no untracked worktree directory is left behind.
    No file inside the stale metadata is changed, including when it is
    owned by another Unix user and read-only to the Gateway process.
    """
    replacement_dir = _normalize_replacement_dir(replacement_dir)
    expected_branch = _normalize_branch(expected_branch)
    expected_current_head = _normalize_sha(expected_current_head, "expected_current_head")
    expected_replacement_head = _normalize_sha(
        expected_replacement_head,
        "expected_replacement_head",
    )
    if expected_current_head == expected_replacement_head:
        raise _error("INVALID_INPUT", "Current and replacement HEAD leases must be different.")

    try:
        project_root = project_root.resolve(strict=True)
    except OSError as exc:
        raise _error("PROJECT_NOT_FOUND", "Registered project root is unavailable.") from exc
    _real_directory(project_root, label="Registered project root")

    parent = project_root.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        raise _error(
            "WORKSPACE_NOT_WRITEABLE",
            "Project parent is not writable for an atomic metadata quarantine.",
        )

    canonical = project_root / ".git"
    replacement = project_root / replacement_dir
    quarantine_id = _quarantine_name(
        project_root,
        expected_current_head,
        expected_replacement_head,
    )
    sibling_quarantine = parent / quarantine_id
    # A project root may be a bind mount while its parent is on another
    # filesystem in the control-plane namespace. Preserve metadata on the
    # same filesystem if the sibling rename fails specifically with EXDEV.
    # Keep the cross-device fallback *inside* the replacement Git directory.
    # Once promoted it becomes .git/<quarantine_id>, so ordinary git status
    # stays clean and the original metadata remains preserved.
    replacement_quarantine = replacement / quarantine_id
    promoted_quarantine = canonical / quarantine_id
    quarantine = sibling_quarantine

    with _repair_lock(journal_root):
        # Reconcile either deterministic location after an ambiguous response.
        # A second quarantine location is always contention, not permission
        # to remove or replace another operator's preserved metadata.
        quarantines = (sibling_quarantine, promoted_quarantine, replacement_quarantine)
        present_quarantines = [
            path for path in quarantines if path.exists() or path.is_symlink()
        ]
        final_identity = None
        if canonical.exists() and not canonical.is_symlink():
            final_identity = _git_identity(canonical, project_root)
        if final_identity == (expected_branch, expected_replacement_head) and not replacement.exists():
            if len(present_quarantines) == 1:
                completed_quarantine = present_quarantines[0]
                if not completed_quarantine.is_symlink():
                    quarantine_identity = _git_identity(completed_quarantine, project_root)
                    if quarantine_identity == (expected_branch, expected_current_head):
                        if not _tracked_worktree_clean(canonical, project_root):
                            raise _error(
                                "WORKSPACE_CONTENDED",
                                "Promoted Git metadata exists but tracked worktree no longer matches it.",
                                retryable=True,
                                details={"mutation_occurred": True},
                            )
                        return ProjectGitMetadataRepairResult(
                            branch=expected_branch,
                            previous_head=expected_current_head,
                            replacement_head=expected_replacement_head,
                            replacement_dir=replacement_dir,
                            quarantine_id=quarantine_id,
                            already_repaired=True,
                            tracked_worktree_clean=True,
                            quarantine_location=(
                                "sibling" if completed_quarantine == sibling_quarantine else "gitdir"
                            ),
                        )

        _real_directory(canonical, label="Canonical .git")
        _real_directory(replacement, label="Replacement Git metadata")
        if present_quarantines:
            raise _error(
                "WORKSPACE_CONTENDED",
                "Deterministic Git metadata quarantine already exists with non-reconcilable state.",
                retryable=False,
                details={"quarantine_id": quarantine_id, "mutation_occurred": False},
            )

        current_identity = _git_identity(canonical, project_root)
        replacement_identity = _git_identity(replacement, project_root)
        if current_identity is None:
            raise _error("CHECK_FAILED", "Canonical .git identity cannot be proven.", retryable=True)
        if replacement_identity is None:
            raise _error("CHECK_FAILED", "Replacement Git identity cannot be proven.", retryable=True)
        if current_identity != (expected_branch, expected_current_head):
            raise _error(
                "WORKSPACE_CONTENDED",
                "Canonical Git branch/HEAD changed since review; refusing metadata replacement.",
                retryable=True,
                details={
                    "expected_branch": expected_branch,
                    "expected_current_head": expected_current_head,
                    "actual_branch": current_identity[0],
                    "actual_current_head": current_identity[1],
                    "mutation_occurred": False,
                },
            )
        if replacement_identity != (expected_branch, expected_replacement_head):
            raise _error(
                "WORKSPACE_CONTENDED",
                "Replacement Git branch/HEAD does not match the reviewed lease.",
                retryable=True,
                details={
                    "expected_branch": expected_branch,
                    "expected_replacement_head": expected_replacement_head,
                    "actual_branch": replacement_identity[0],
                    "actual_replacement_head": replacement_identity[1],
                    "mutation_occurred": False,
                },
            )
        if not _tracked_worktree_clean(replacement, project_root):
            raise _error(
                "WORKSPACE_CONTENDED",
                "Tracked worktree does not exactly match replacement Git metadata.",
                retryable=False,
                details={"mutation_occurred": False},
            )

        moved_old = False
        moved_replacement = False
        failed_stage = "quarantine_sibling"
        try:
            try:
                os.rename(canonical, quarantine)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # Never leave a Git metadata archive untracked in the worktree.
                # The replacement .git and old .git live on the same filesystem;
                # the archive moves with the replacement on promotion.
                quarantine = replacement_quarantine
                failed_stage = "quarantine_gitdir"
                os.rename(canonical, quarantine)
            moved_old = True
            failed_stage = "promote_replacement"
            os.rename(replacement, canonical)
            if quarantine == replacement_quarantine:
                quarantine = promoted_quarantine
            moved_replacement = True
            failed_stage = "fsync"
            _fsync_directory(project_root)
            _fsync_directory(parent)
        except OSError as exc:
            rollback_ok = False
            try:
                if moved_replacement and canonical.exists() and not replacement.exists():
                    os.rename(canonical, replacement)
                    if quarantine == promoted_quarantine:
                        quarantine = replacement_quarantine
                if moved_old and quarantine.exists() and not canonical.exists():
                    os.rename(quarantine, canonical)
                rollback_ok = canonical.exists() and replacement.exists() and not quarantine.exists()
                _fsync_directory(project_root)
                _fsync_directory(parent)
            except OSError:
                rollback_ok = False
            if not rollback_ok:
                raise _error(
                    "INTERNAL_ERROR",
                    "Git metadata replacement failed and automatic rollback was incomplete.",
                    details={"mutation_occurred": "unknown", "quarantine_id": quarantine_id},
                ) from exc
            raise _error(
                "TOOL_EXECUTION_FAILED",
                "Git metadata replacement failed; original metadata was restored.",
                retryable=True,
                details={
                    "mutation_occurred": False,
                    "failed_stage": failed_stage,
                    "os_errno": errno.errorcode.get(exc.errno if exc.errno is not None else -1, "UNKNOWN"),
                },
            ) from exc

        final_identity = _git_identity(canonical, project_root)
        final_clean = _tracked_worktree_clean(canonical, project_root)
        if final_identity != (expected_branch, expected_replacement_head) or not final_clean:
            rollback_ok = False
            try:
                if canonical.exists() and not replacement.exists():
                    os.rename(canonical, replacement)
                    if quarantine == promoted_quarantine:
                        quarantine = replacement_quarantine
                if quarantine.exists() and not canonical.exists():
                    os.rename(quarantine, canonical)
                rollback_ok = canonical.exists() and replacement.exists() and not quarantine.exists()
                _fsync_directory(project_root)
                _fsync_directory(parent)
            except OSError:
                rollback_ok = False
            if not rollback_ok:
                raise _error(
                    "INTERNAL_ERROR",
                    "Git metadata postcondition failed and automatic rollback was incomplete.",
                    details={"mutation_occurred": "unknown", "quarantine_id": quarantine_id},
                )
            raise _error(
                "WORKSPACE_VERIFICATION_FAILED",
                "Git metadata replacement postconditions failed; original metadata was restored.",
                retryable=True,
                details={"mutation_occurred": False},
            )

        return ProjectGitMetadataRepairResult(
            branch=expected_branch,
            previous_head=expected_current_head,
            replacement_head=expected_replacement_head,
            replacement_dir=replacement_dir,
            quarantine_id=quarantine_id,
            already_repaired=False,
            tracked_worktree_clean=True,
            quarantine_location=(
                "sibling" if quarantine == sibling_quarantine else "gitdir"
            ),
        )