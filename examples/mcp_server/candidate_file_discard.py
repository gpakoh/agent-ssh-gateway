"""Single-file, fail-closed managed candidate discard with durable content preservation.

This is deliberately not a generic project delete API. It only accepts an
exact managed candidate clone with one untracked regular file. The bytes are
archived outside the workspace *before* the original directory entry is
removed, and all existing candidate cleanup/reference guards still apply.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from examples.mcp_server import candidate_clone as clone
from examples.mcp_server.project_registry_control import project_registry_mutation_lock

_MAX_BYTES = 1024 * 1024
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HEX_SHA256 = re.compile(r"[a-f0-9]{64}")
_FULL_HEAD = re.compile(r"[a-f0-9]{40}")
_LEGACY_PLAN = ".ai-bridge/current-plan.md"


def _deny(message: str, *, code: str = "WORKSPACE_CONTENDED") -> None:
    raise clone.CandidateCloneError(code, message)


def _checked_path(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 512:
        _deny("relative_path must be a bounded relative file name", code="INVALID_INPUT")
    # Narrow exception: the legacy plan may be archived before delivery, but
    # arbitrary hidden paths remain forbidden. Separate evidence guards below
    # must prove this is the sole inactive handoff file.
    if value == _LEGACY_PLAN:
        return (".ai-bridge", "current-plan.md")
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
            clone._git_command(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"]),
            cwd=root, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise clone.CandidateCloneError("TOOL_EXECUTION_FAILED", "candidate Git status is unavailable") from exc
    if result.returncode != 0:
        _deny("candidate Git status is unavailable")
    if result.stdout != b"?? " + relative_path.encode("ascii") + b"\x00":
        _deny("candidate has other changes or file is not exactly untracked")


def _require_idle_evidence(root: Path, path: str, expected_sha: str, expected_size: int) -> None:
    evidence = clone._candidate_active_evidence_details(root)
    if path != _LEGACY_PLAN:
        if evidence is not None:
            _deny("candidate has active task or delivery evidence")
        return
    if (
        evidence is None
        or evidence.get("evidence_root_type") != "directory"
        or evidence.get("plan_type") != "regular"
        or evidence.get("plan_fingerprint_status") != "verified"
        or evidence.get("plan_sha256") != expected_sha
        or evidence.get("plan_size_bytes") != expected_size
        or evidence.get("tasks_type") not in {"absent", "directory"}
        or evidence.get("task_entries") != []
        or evidence.get("task_entries_truncated") is not False
    ):
        _deny("legacy plan is not the exact inactive handoff evidence")
    if clone._run_git(root, ["ls-files", "-z", "--", ".ai-bridge"], operation="verify untracked handoff"):
        _deny("legacy handoff contains tracked evidence")
    clone._require_tracked_files_match_head(root)


def _require_plan_only_layout(parent_fd: int) -> None:
    # Check even ignored entries; empty task/archive directories are harmless.
    with os.scandir(parent_fd) as entries:
        for entry in entries:
            if entry.name == "current-plan.md" and entry.is_file(follow_symlinks=False):
                continue
            if entry.name in {"tasks", "archive"} and entry.is_dir(follow_symlinks=False):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    with os.scandir(child) as children:
                        if next(children, None) is None:
                            continue
                finally:
                    os.close(child)
            _deny("legacy handoff contains additional or unsafe evidence")


def _read_exact_file(
    parent_fd: int, name: str, expected_sha: str, expected_size: int, *,
    max_bytes: int = _MAX_BYTES,
) -> tuple[bytes, os.stat_result]:
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
            or before.st_size > max_bytes
        ):
            _deny("untracked file type, ownership, links or size mismatched")
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(data)))
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


def _archive(
    journal_root: Path, archive_id: str, data: bytes, *, max_bytes: int = _MAX_BYTES,
) -> None:
    """Persist exact bytes and their directory entry before destructive action."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(journal_root, flags)
    except OSError as exc:
        raise clone.CandidateCloneError("AUDIT_UNAVAILABLE", "candidate archive unsafe") from exc
    try:
        try:
            os.mkdir("candidate-file-archive", 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            archive_fd = os.open("candidate-file-archive", flags, dir_fd=root_fd)
        except OSError as exc:
            raise clone.CandidateCloneError("AUDIT_UNAVAILABLE", "candidate archive unsafe") from exc
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
            except FileExistsError as exc:
                # An earlier attempt may already have removed a file. Never
                # treat archived bytes as authority to delete a recreated file.
                raise clone.CandidateCloneError(
                    "WORKSPACE_CONTENDED",
                    "candidate archive already exists; reconcile prior discard before retry",
                ) from exc
            try:
                offset = 0
                while offset < len(data):
                    written = os.write(fd, data[offset:])
                    if written <= 0:
                        _deny("candidate archive write made no progress", code="AUDIT_UNAVAILABLE")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(archive_fd)
            # Verify the exact archive; do not block on an unexpected FIFO.
            check_fd = os.open(
                name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=archive_fd,
            )
            try:
                found = os.fstat(check_fd)
                if not stat.S_ISREG(found.st_mode) or found.st_nlink != 1 or found.st_uid != os.geteuid() or found.st_mode & 0o077 or found.st_size != len(data):
                    _deny("candidate archive identity mismatched", code="AUDIT_UNAVAILABLE")
                actual = bytearray()
                while len(actual) <= max_bytes:
                    part = os.read(check_fd, min(65536, max_bytes + 1 - len(actual)))
                    if not part:
                        break
                    actual.extend(part)
                if bytes(actual) != data:
                    _deny("candidate archive content mismatched", code="AUDIT_UNAVAILABLE")
            finally:
                os.close(check_fd)
            # fsync of the child directory does not persist its own entry in
            # the parent. Always sync the parent, including a pre-existing
            # archive directory left by a previous interrupted operation.
            os.fsync(root_fd)
        finally:
            os.close(archive_fd)
    except OSError as exc:
        raise clone.CandidateCloneError("AUDIT_UNAVAILABLE", "candidate archive persistence unavailable") from exc
    finally:
        os.close(root_fd)


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
            _require_idle_evidence(candidate_root, relative_path, expected_sha256, expected_size)
            clone._require_no_inbound_candidate_dependencies(candidate_root.parent, candidate_root)
            _exact_status(candidate_root, relative_path)
            reference_guard()  # Must verify external active PR/agent refs and durable audit.
            parent_fd = _open_parent(candidate_root, parts)
            try:
                if relative_path == _LEGACY_PLAN:
                    _require_plan_only_layout(parent_fd)
                data, file_stat = _read_exact_file(parent_fd, parts[-1], expected_sha256, expected_size)
                _archive(journal_root, archive_id, data)
                _exact_status(candidate_root, relative_path)
                reference_guard()
                if relative_path == _LEGACY_PLAN:
                    _require_idle_evidence(candidate_root, relative_path, expected_sha256, expected_size)
                    _require_plan_only_layout(parent_fd)
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


# The single-file API retains its original limit and semantics. A batch is a
# separately bounded operation with a durable plan and archive-all barrier.
_BATCH_MAX_FILES = 8
_BATCH_MAX_FILE_BYTES = 8 * 1024 * 1024
_BATCH_MAX_TOTAL_BYTES = 16 * 1024 * 1024


def _git_bytes(root: Path, args: list[str]) -> bytes:
    try:
        result = subprocess.run(
            clone._git_command(root, args), cwd=root, capture_output=True,
            timeout=30, check=False, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise clone.CandidateCloneError("CHECK_FAILED", "candidate Git observation unavailable") from exc
    if result.returncode != 0:
        _deny("candidate Git observation unavailable")
    return result.stdout


@contextlib.contextmanager
def _batch_git_index_lock(root: Path, operation_id: str) -> Iterator[Callable[[], None]]:
    """Hold Git's actual index interlock, not a parallel advisory lock.

    Ordinary Git index writers must fail while archive/quarantine is in
    progress. This does not fence direct filesystem writers or remote
    delivery, so the existing active-reference prohibition still applies.
    """
    git_dir = root / ".git"
    actual_dir = Path(os.fsdecode(_git_bytes(root, ["rev-parse", "--absolute-git-dir"])).rstrip("\n"))
    index = Path(os.fsdecode(_git_bytes(root, ["rev-parse", "--git-path", "index"])).rstrip("\n"))
    if not index.is_absolute():
        index = root / index
    if actual_dir != git_dir or index != git_dir / "index":
        _deny("candidate Git index path is not the managed clone index")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        git_fd = os.open(git_dir, flags)
    except OSError as exc:
        raise clone.CandidateCloneError("WORKSPACE_CONTENDED", "candidate Git directory unavailable") from exc
    try:
        directory = os.fstat(git_fd)
        try:
            index_stat = os.stat("index", dir_fd=git_fd, follow_symlinks=False)
            if not stat.S_ISREG(index_stat.st_mode) or index_stat.st_nlink != 1:
                _deny("candidate Git index path is unsafe")
            lock_fd = os.open(
                "index.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=git_fd,
            )
        except OSError as exc:
            raise clone.CandidateCloneError(
                "WORKSPACE_CONTENDED", "candidate Git index lock unavailable", retryable=True,
            ) from exc

        def assert_owned() -> None:
            try:
                current_dir = git_dir.lstat()
                held = os.fstat(lock_fd)
                visible = os.stat("index.lock", dir_fd=git_fd, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(current_dir.st_mode)
                    or (current_dir.st_dev, current_dir.st_ino) != (directory.st_dev, directory.st_ino)
                    or not stat.S_ISREG(visible.st_mode)
                    or (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino)
                    or held.st_nlink != 1
                    or held.st_uid != os.geteuid()
                    or held.st_size != 0
                ):
                    raise OSError("index lock identity changed")
            except OSError as exc:
                raise clone.CandidateCloneError(
                    "WORKSPACE_CONTENDED", "candidate Git index lock ownership lost",
                    retryable=False, details={"operation_id": operation_id, "outcome": "partial_or_unknown"},
                ) from exc

        try:
            assert_owned()
            yield assert_owned
        finally:
            try:
                # Never remove a pre-existing or replaced lock. An interrupted
                # operation's own stale lock requires explicit reconciliation.
                assert_owned()
                try:
                    os.unlink("index.lock", dir_fd=git_fd)
                except OSError as exc:
                    raise clone.CandidateCloneError(
                        "WORKSPACE_CONTENDED", "candidate Git index lock release failed",
                        retryable=False, details={"operation_id": operation_id, "outcome": "partial_or_unknown"},
                    ) from exc
            finally:
                os.close(lock_fd)
    finally:
        os.close(git_fd)


def _batch_state(
    root: Path, branch: str, head: str, index_sha: str,
    paths: list[str], *, status_sha: str | None = None,
) -> None:
    if clone._run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"], operation="candidate branch") != branch:
        _deny("candidate branch changed")
    if clone._run_git(root, ["rev-parse", "HEAD"], operation="candidate HEAD") != head:
        _deny("candidate HEAD changed")
    index = _git_bytes(root, ["ls-files", "--stage", "-z"])
    if hashlib.sha256(index).hexdigest() != index_sha:
        _deny("candidate index changed")
    status = _git_bytes(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"])
    expected = sorted(b"?? " + path.encode("ascii") for path in paths)
    if sorted(status.split(b"\x00")[:-1]) != expected or (status and not status.endswith(b"\x00")):
        _deny("candidate status is not the exact requested untracked set")
    if status_sha is not None:
        visible = _git_bytes(root, ["status", "--porcelain=v1"])
        if hashlib.sha256(visible).hexdigest() != status_sha:
            _deny("candidate status fingerprint changed")
    clone._require_tracked_files_match_head(root)


def _batch_specs(files: list[dict[str, Any]]) -> list[tuple[str, str, int, tuple[str, ...]]]:
    if not isinstance(files, list) or not 2 <= len(files) <= _BATCH_MAX_FILES:
        _deny("batch requires 2..8 exact files", code="INVALID_INPUT")
    specs = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"relative_path", "expected_sha256", "expected_size"}:
            _deny("each batch file requires exact path, SHA256 and size", code="INVALID_INPUT")
        path, digest, size = item["relative_path"], item["expected_sha256"], item["expected_size"]
        parts = _checked_path(path)
        if path == _LEGACY_PLAN:
            _deny("legacy handoff is single-file only", code="INVALID_INPUT")
        if not isinstance(digest, str) or not _HEX_SHA256.fullmatch(digest):
            _deny("invalid batch file SHA256", code="INVALID_INPUT")
        if type(size) is not int or not 0 <= size <= _BATCH_MAX_FILE_BYTES:
            _deny("batch file size exceeds 8MiB", code="INVALID_INPUT")
        specs.append((path, digest, size, parts))
    if len({item[0] for item in specs}) != len(specs) or sum(item[2] for item in specs) > _BATCH_MAX_TOTAL_BYTES:
        _deny("duplicate batch path or total size exceeds 16MiB", code="INVALID_INPUT")
    return sorted(specs)


def discard_untracked_candidate_files(
    project_id: str,
    expected_source_project: str,
    expected_branch: str,
    expected_head_sha: str,
    expected_index_sha256: str,
    expected_status_sha256: str,
    files: list[dict[str, Any]],
    *,
    config_dir: Path,
    journal_root: Path,
    reference_guard: Callable[[], None],
) -> dict[str, Any]:
    """Archive the entire exact untracked set before quarantining any entry.

    This is not an atomic multi-file delete. The persistent plan identifies
    every archive and quarantine path, including after a process crash.
    An ambiguous/partial result must be reconciled, never replayed.
    """
    specs = _batch_specs(files)
    if not isinstance(project_id, str) or not clone._PROJECT_ID_RE.fullmatch(project_id):
        _deny("invalid managed candidate project_id", code="INVALID_INPUT")
    branch = clone._validate_branch(expected_branch)
    if not isinstance(expected_source_project, str) or not expected_source_project.strip():
        _deny("expected_source_project must be supplied", code="INVALID_INPUT")
    if not isinstance(expected_head_sha, str) or not _FULL_HEAD.fullmatch(expected_head_sha):
        _deny("invalid expected HEAD", code="INVALID_INPUT")
    if any(not isinstance(value, str) or not _HEX_SHA256.fullmatch(value) for value in (
        expected_index_sha256, expected_status_sha256,
    )):
        _deny("exact index and status SHA256 are required", code="INVALID_INPUT")
    if not callable(reference_guard):
        _deny("reference_guard is required", code="INVALID_INPUT")

    config_dir, journal_root = config_dir.resolve(), journal_root.resolve()
    workspace = clone._workspace_root(config_dir)
    root = clone._candidate_clones_root(workspace) / project_id
    paths = [item[0] for item in specs]
    identity = {
        "project_id": project_id, "source_project": expected_source_project,
        "branch": branch, "head": expected_head_sha,
        "index_sha256": expected_index_sha256, "status_sha256": expected_status_sha256,
        "files": [{"path": p, "sha256": h, "size": n} for p, h, n, _ in specs],
    }
    operation_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with clone._lineage_lock(workspace, expected_source_project, branch):
        with project_registry_mutation_lock(journal_root), contextlib.ExitStack() as stack:
            def verify_identity() -> None:
                if not clone._registry_has_candidate(
                    config_dir, project_id, expected_root=f".mcp-candidate-clones/{project_id}",
                ):
                    _deny("managed candidate is not registered")
                clone._validate_candidate_root(root, root.parent)
                clone._reject_symlinked_git_dir(root)
                metadata = clone._read_metadata(root)
                if any(metadata.get(k) != v for k, v in {
                    "project_id": project_id, "source_project": expected_source_project,
                    "branch": branch, "root": f".mcp-candidate-clones/{project_id}",
                }.items()):
                    _deny("candidate metadata identity mismatched")
                if clone._candidate_active_evidence_details(root) is not None:
                    _deny("candidate has active task or delivery evidence")
                clone._require_no_inbound_candidate_dependencies(root.parent, root)
                _batch_state(root, branch, expected_head_sha, expected_index_sha256, paths,
                             status_sha=expected_status_sha256)

            verify_identity()
            assert_index_lock = stack.enter_context(_batch_git_index_lock(root, operation_id))
            verify_identity()
            reference_guard()
            assert_index_lock()
            records: list[dict[str, Any]] = []
            moved: list[dict[str, Any]] = []
            removed: list[str] = []
            planned = False
            try:
                for path, digest, size, parts in specs:
                    fd = _open_parent(root, parts)
                    try:
                        data, st = _read_exact_file(fd, parts[-1], digest, size, max_bytes=_BATCH_MAX_FILE_BYTES)
                        record = {
                            "path": path, "name": parts[-1], "sha256": digest, "size": size,
                            "fd": fd, "data": data, "stat": st,
                            "archive_id": hashlib.sha256(("\x00".join((
                                project_id, branch, expected_head_sha, path, digest,
                            ))).encode()).hexdigest(),
                            "quarantine": ".mcp-discard-" + secrets.token_hex(16),
                        }
                        records.append(record)
                    except Exception:
                        os.close(fd)
                        raise
                plan = {
                    **identity, "operation_id": operation_id, "phase": "prepared",
                    "entries": [{k: r[k] for k in (
                        "path", "sha256", "size", "archive_id", "quarantine",
                    )} for r in records],
                }
                # Exclusive persistent plan denies replay even if a file has
                # been recreated byte-identically after a partial operation.
                _archive(journal_root, operation_id, json.dumps(plan, sort_keys=True).encode())
                planned = True
                for r in records:
                    _archive(journal_root, r["archive_id"], r["data"], max_bytes=_BATCH_MAX_FILE_BYTES)
                # No original entry has been moved or removed before here.
                verify_identity()
                reference_guard()
                assert_index_lock()
                verify_identity()
                for r in records:
                    assert_index_lock()
                    data, st = _read_exact_file(r["fd"], r["name"], r["sha256"], r["size"], max_bytes=_BATCH_MAX_FILE_BYTES)
                    if data != r["data"] or (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns) != (
                        r["stat"].st_dev, r["stat"].st_ino, r["stat"].st_mtime_ns, r["stat"].st_ctime_ns,
                    ):
                        _deny("batch file changed before quarantine")
                    os.rename(r["name"], r["quarantine"], src_dir_fd=r["fd"], dst_dir_fd=r["fd"])
                    moved.append(r)
                    os.fsync(r["fd"])
                # Recheck every quarantined inode before any final unlink.
                def verify_quarantine(r: dict[str, Any]) -> None:
                    _, st = _read_exact_file(r["fd"], r["quarantine"], r["sha256"], r["size"], max_bytes=_BATCH_MAX_FILE_BYTES)
                    if (st.st_dev, st.st_ino, st.st_mtime_ns) != (
                        r["stat"].st_dev, r["stat"].st_ino, r["stat"].st_mtime_ns,
                    ):
                        _deny("batch file changed during quarantine")
                    try:
                        os.stat(r["name"], dir_fd=r["fd"], follow_symlinks=False)
                    except FileNotFoundError:
                        return
                    _deny("batch original pathname was recreated")

                for r in moved:
                    verify_quarantine(r)
                reference_guard()
                assert_index_lock()
                quarantined_paths = [str(Path(r["path"]).with_name(r["quarantine"])) for r in moved]
                _batch_state(root, branch, expected_head_sha, expected_index_sha256, quarantined_paths)
                for r in moved:
                    assert_index_lock()
                    verify_quarantine(r)
                    os.unlink(r["quarantine"], dir_fd=r["fd"])
                    removed.append(r["path"])
                    os.fsync(r["fd"])
                assert_index_lock()
                _batch_state(root, branch, expected_head_sha, expected_index_sha256, [])
                receipt = {
                    "project_id": project_id, "head": expected_head_sha,
                    "operation_id": operation_id, "discarded": True,
                    "files": plan["entries"], "removed": removed,
                }
                receipt_id = hashlib.sha256((operation_id + ":completed").encode()).hexdigest()
                _archive(journal_root, receipt_id, json.dumps(receipt, sort_keys=True).encode())
                return receipt
            except Exception as exc:
                # Restore only when the original name is absent, atomically
                # without replacing a concurrent writer's newly created file.
                # Never discard unknown or changed quarantined data.
                for r in reversed(moved):
                    if r["path"] in removed:
                        continue
                    try:
                        os.link(r["quarantine"], r["name"], src_dir_fd=r["fd"], dst_dir_fd=r["fd"], follow_symlinks=False)
                        os.unlink(r["quarantine"], dir_fd=r["fd"])
                        os.fsync(r["fd"])
                    except OSError:
                        pass
                if planned:
                    raise clone.CandidateCloneError(
                        "WORKSPACE_CONTENDED", "batch discard incomplete; reconcile durable plan before retry",
                        retryable=False, details={"operation_id": operation_id, "removed": removed,
                                                 "outcome": "partial_or_unknown"},
                    ) from exc
                if isinstance(exc, clone.CandidateCloneError):
                    raise
                raise clone.CandidateCloneError("WORKSPACE_CONTENDED", "batch discard preflight failed") from exc
            finally:
                for r in records:
                    os.close(r["fd"])
