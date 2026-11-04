"""Single-file, fail-closed managed candidate discard with durable content preservation.

This is deliberately not a generic project delete API. It only accepts an
exact managed candidate clone with one untracked regular file. The bytes are
archived outside the workspace *before* the original directory entry is
removed, and all existing candidate cleanup/reference guards still apply.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from examples.mcp_server import candidate_clone as clone
from examples.mcp_server.project_registry_control import project_registry_mutation_lock

_MAX_BYTES = 1024 * 1024
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HEX_SHA256 = re.compile(r"[a-f0-9]{64}")
_FULL_HEAD = re.compile(r"[a-f0-9]{40}")


def _deny(message: str, *, code: str = "WORKSPACE_CONTENDED") -> None:
    raise clone.CandidateCloneError(code, message)


def _checked_path(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 512:
        _deny("relative_path must be a bounded relative file name", code="INVALID_INPUT")
    parts = value.split("/")
    if (
        len(parts) > 6
        or any(not _SAFE_SEGMENT.fullmatch(part) for part in parts)
        or any(part in {".", "..", ".git", ".ai-bridge"} for part in parts)
    ):
        _deny("relative_path must not escape or contain unsafe components", code="INVALID_INPUT")
    return tuple(parts)


def _exact_status(root: Path, relative_path: str) -> None:
    """Reject all tracked/other untracked work; inspect unquoted NUL-delimited Git status."""
    try:
        result = subprocess.run(
            clone._git_command(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]),
            cwd=root, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise clone.CandidateCloneError("TOOL_EXECUTION_FAILED", "candidate Git status is unavailable") from exc
    if result.returncode != 0:
        _deny("candidate Git status is unavailable")
    if result.stdout != b"?? " + relative_path.encode("ascii") + b"\x00":
        _deny("candidate has other changes or file is not exactly untracked")


def _read_exact_file(parent_fd: int, name: str, expected_sha: str, expected_size: int) -> tuple[bytes, os.stat_result]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        _deny("no-follow filesystem operations are not supported", code="POLICY_DENIED")
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NONBLOCK | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise clone.CandidateCloneError("WORKSPACE_CONTENDED", "untracked file cannot be opened safely") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size != expected_size
            or before.st_size > _MAX_BYTES
        ):
            _deny("untracked file type, ownership, links or size mismatched")
        data = bytearray()
        while len(data) <= _MAX_BYTES:
            chunk = os.read(fd, min(65536, _MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        if (
            len(data) != expected_size
            or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns, before.st_size)
            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_size)
            or hashlib.sha256(data).hexdigest() != expected_sha
        ):
            _deny("untracked file changed or expected content SHA256 mismatched")
        return bytes(data), after
    finally:
        os.close(fd)


def _open_parent(candidate_root: Path, segments: tuple[str, ...]) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(candidate_root, flags)
        for part in segments[:-1]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise clone.CandidateCloneError("WORKSPACE_CONTENDED", "untracked file parent is unsafe") from exc


def _archive(journal_root: Path, archive_id: str, data: bytes) -> None:
    """Persist exact bytes under an owner-only, nofollow archive directory."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        os.mkdir(journal_root / "candidate-file-archive", 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise clone.CandidateCloneError("AUDIT_UNAVAILABLE", "candidate archive unavailable") from exc
    try:
        root_fd = os.open(journal_root, flags)
        archive_fd = os.open("candidate-file-archive", flags, dir_fd=root_fd)
    except OSError as exc:
        raise clone.CandidateCloneError("AUDIT_UNAVAILABLE", "candidate archive unsafe") from exc
    finally:
        if "root_fd" in locals():
            os.close(root_fd)
    try:
        directory_stat = os.fstat(archive_fd)
        if directory_stat.st_uid != os.geteuid() or directory_stat.st_mode & 0o077:
            _deny("candidate archive permissions unsafe", code="AUDIT_UNAVAILABLE")
        name = archive_id + ".blob"
        try:
            fd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                dir_fd=archive_fd,
            )
        except FileExistsError:
            fd = None
        except OSError as exc:
            raise clone.CandidateCloneError("AUDIT_UNAVAILABLE", "candidate archive write unavailable") from exc
        if fd is not None:
            try:
                offset = 0
                while offset < len(data):
                    offset += os.write(fd, data[offset:])
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(archive_fd)
        # Refuse an existing corrupt/colliding archive rather than overwrite it.
        try:
            check_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=archive_fd)
        except OSError as exc:
            raise clone.CandidateCloneError("AUDIT_UNAVAILABLE", "candidate archive read unavailable") from exc
        try:
            found = os.fstat(check_fd)
            if not stat.S_ISREG(found.st_mode) or found.st_nlink != 1 or found.st_uid != os.geteuid() or found.st_mode & 0o077 or found.st_size != len(data):
                _deny("candidate archive identity mismatched", code="AUDIT_UNAVAILABLE")
            actual = bytearray()
            while len(actual) <= _MAX_BYTES:
                part = os.read(check_fd, min(65536, _MAX_BYTES + 1 - len(actual)))
                if not part:
                    break
                actual.extend(part)
            if bytes(actual) != data:
                _deny("candidate archive content mismatched", code="AUDIT_UNAVAILABLE")
        finally:
            os.close(check_fd)
    finally:
        os.close(archive_fd)


def discard_untracked_candidate_file(
    project_id: str,
    expected_source_project: str,
    expected_branch: str,
    expected_head_sha: str,
    relative_path: str,
    expected_sha256: str,
    expected_size: int,
    *,
    config_dir: Path,
    journal_root: Path,
    reference_guard: Callable[[], None],
) -> dict[str, Any]:
    """Archive then atomically quarantine and remove exactly one untracked file."""
    parts = _checked_path(relative_path)
    if not isinstance(project_id, str) or not clone._PROJECT_ID_RE.fullmatch(project_id):
        _deny("invalid managed candidate project_id", code="INVALID_INPUT")
    expected_branch = clone._validate_branch(expected_branch)
    if not isinstance(expected_source_project, str) or not expected_source_project.strip():
        _deny("expected_source_project must be supplied", code="INVALID_INPUT")
    if not isinstance(expected_head_sha, str) or not _FULL_HEAD.fullmatch(expected_head_sha):
        _deny("expected_head_sha must be a full lowercase commit SHA", code="INVALID_INPUT")
    if not isinstance(expected_sha256, str) or not _HEX_SHA256.fullmatch(expected_sha256):
        _deny("expected_sha256 must be a full lowercase SHA256", code="INVALID_INPUT")
    if type(expected_size) is not int or not 0 <= expected_size <= _MAX_BYTES:
        _deny("expected_size is outside 0..1MiB", code="INVALID_INPUT")
    if not callable(reference_guard):
        _deny("reference_guard is required", code="INVALID_INPUT")

    config_dir = config_dir.resolve()
    journal_root = journal_root.resolve()
    workspace_root = clone._workspace_root(config_dir)
    candidate_root = clone._candidate_clones_root(workspace_root) / project_id
    archive_id = hashlib.sha256(
        ("\x00".join((project_id, expected_branch, expected_head_sha, relative_path, expected_sha256))).encode()
    ).hexdigest()

    with clone._lineage_lock(workspace_root, expected_source_project, expected_branch):
        with project_registry_mutation_lock(journal_root):
            if not clone._registry_has_candidate(
                config_dir, project_id,
                expected_root=f".mcp-candidate-clones/{project_id}",
            ):
                _deny("managed candidate is not registered")
            clone._validate_candidate_root(candidate_root, candidate_root.parent)
            clone._reject_symlinked_git_dir(candidate_root)
            metadata = clone._read_metadata(candidate_root)
            if any(metadata.get(key) != value for key, value in {
                "project_id": project_id,
                "source_project": expected_source_project,
                "branch": expected_branch,
                "root": f".mcp-candidate-clones/{project_id}",
            }.items()):
                _deny("candidate metadata identity mismatched")
            if clone._run_git(candidate_root, ["rev-parse", "--abbrev-ref", "HEAD"], operation="candidate branch") != expected_branch:
                _deny("candidate branch changed")
            if clone._run_git(candidate_root, ["rev-parse", "HEAD"], operation="candidate HEAD") != expected_head_sha:
                _deny("candidate HEAD changed")
            if clone._candidate_active_evidence_details(candidate_root) is not None:
                _deny("candidate has active task or delivery evidence")
            clone._require_no_inbound_candidate_dependencies(candidate_root.parent, candidate_root)
            _exact_status(candidate_root, relative_path)
            reference_guard()  # Must verify external active PR/agent refs and durable audit.
            parent_fd = _open_parent(candidate_root, parts)
            try:
                data, file_stat = _read_exact_file(parent_fd, parts[-1], expected_sha256, expected_size)
                _archive(journal_root, archive_id, data)
                _exact_status(candidate_root, relative_path)
                reference_guard()
                # Rename is atomic in the same directory; never unlink the
                # original pathname (which could have been swapped after CAS).
                quarantine = ".mcp-discard-" + secrets.token_hex(16)
                os.rename(parts[-1], quarantine, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                removed = False
                try:
                    moved = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
                    if (
                        moved.st_dev != file_stat.st_dev or moved.st_ino != file_stat.st_ino
                        or not stat.S_ISREG(moved.st_mode) or moved.st_nlink != 1
                    ):
                        _deny("untracked file swapped during quarantine")
                    # The exact entry moved by rename must still contain the
                    # archived bytes. A writer holding an open fd could have
                    # changed it after the initial read and before quarantine.
                    _data_again, checked = _read_exact_file(
                        parent_fd, quarantine, expected_sha256, expected_size
                    )
                    # A same-directory rename legitimately changes ctime;
                    # compare mtime and exact bytes instead.
                    if checked.st_mtime_ns != file_stat.st_mtime_ns:
                        _deny("untracked file changed before final removal")
                    os.unlink(quarantine, dir_fd=parent_fd)
                    removed = True
                    os.fsync(parent_fd)
                except Exception:
                    # Never delete unexpected content. Restore it if safe;
                    # otherwise retain the quarantined bytes for review. After
                    # unlink, the archived bytes are the recovery source.
                    if not removed:
                        try:
                            os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            os.rename(
                                quarantine, parts[-1],
                                src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                            )
                    raise
            except clone.CandidateCloneError:
                raise
            except OSError as exc:
                raise clone.CandidateCloneError("WORKSPACE_CONTENDED", "guarded untracked file discard failed", retryable=True) from exc
            finally:
                os.close(parent_fd)
    return {
        "project_id": project_id,
        "head": expected_head_sha,
        "relative_path": relative_path,
        "discarded": True,
        "archive_id": archive_id,
        "archived_sha256": expected_sha256,
        "archived_size": expected_size,
    }
