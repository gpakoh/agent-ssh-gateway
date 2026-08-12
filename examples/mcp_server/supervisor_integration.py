"""Supervisor-only single-file integration primitive (MCP layer).

This is a narrow persistence primitive for supervisor-driven integration:
safely replace the content of ONE existing regular file in place, even
when the parent directory is not writable, without weakening
``app/services/project_patch.py`` and without being registered with
FastMCP yet.

Design
------
* The target is opened once with ``O_RDWR | O_NOFOLLOW`` and all
  preconditions (regular single-link inode, lstat/fstat identity,
  content hash) are verified THROUGH the same pinned descriptor, so no
  rename/tmp-file-in-parent-dir is needed and the file mode/owner are
  preserved.
* A per-target advisory ``flock`` serialises writers; every precondition
  is re-validated after the lock is acquired so waiting on the lock can
  never stale the check.
* Before mutating the target we persist a byte-exact backup and a crash
  journal under ``journal_root`` (mode 0700; files 0600), fsync the
  backup, the journal and the journal directory, and only then write the
  new bytes through the pinned fd (full write loop + ftruncate + fsync).
* On any write exception the original bytes are restored through the same
  pinned fd; if that rollback also fails, a distinct ``RollbackFailedError``
  is raised and the journal is retained for manual intervention.

Crash recovery
--------------
``recover_pending()`` first type-validates every journal field it consumes
(version as a supported integer, project/relative/target paths as strings,
dev/ino as ints rejecting bools, hashes as valid sha256 strings, backup as
the expected safe basename); a malformed or unsupported-version journal
fails closed and is retained without aborting the sweep. For each valid
journal it then inspects the current target hash: new hash => prior
integration completed (cleanup); original hash => no mutation happened
(cleanup); any other hash => partial write, so the byte-exact backup is
restored through a pinned fd, fsynced, re-read and hash-verified against
``original_hash`` before cleanup. If the path/inode changed or the target
is unsafe it fails closed and retains the journal.

Only in-place replacement of ONE existing regular single-link file is
supported; new files, deletes, symlink targets and multi-file batches are
explicitly rejected.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_SHA256_PREFIX = "sha256:"
_SHA256_HEX_LEN = 64
_JOURNAL_VERSION = 1
_READ_CHUNK = 64 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", None)


class SupervisorIntegrationError(Exception):
    """Base error for the supervisor integration primitive."""


class PathEscapeError(SupervisorIntegrationError):
    """The relative path is absolute or escapes the project root."""


class TargetMissingError(SupervisorIntegrationError):
    """The target file does not exist."""


class SymlinkRejectedError(SupervisorIntegrationError):
    """The target is a symlink; symlink targets are unsupported."""


class UnsupportedTargetError(SupervisorIntegrationError):
    """The target is not a regular single-link file."""


class HashMismatchError(SupervisorIntegrationError):
    """The current content hash does not match the mandatory expected hash."""


class JournalPendingError(SupervisorIntegrationError):
    """A pending recovery journal exists for this target; run recover_pending() first."""


class RollbackFailedError(SupervisorIntegrationError):
    """The write failed and restoring the original bytes also failed; the journal is retained."""


@dataclass(frozen=True)
class IntegrateResult:
    """Outcome of a successful ``integrate_file`` call."""

    relative_path: str
    target_path: str
    original_hash: str
    new_hash: str


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of recovering one pending journal via ``recover_pending``.

    ``status`` is one of:
    * ``completed`` - the integration had already finished (current hash == new hash).
    * ``unchanged`` - no mutation happened (current hash == original hash).
    * ``restored`` - a partial write was repaired from the byte-exact backup.
    * ``error`` - failed closed; ``journal_retained`` is True for manual intervention.
    """

    relative_path: str
    status: Literal["completed", "unchanged", "restored", "error"]
    journal_retained: bool
    error: str | None = None


@dataclass(frozen=True)
class _TargetHandle:
    target: Path
    fd: int
    lstat: os.stat_result
    fstat: os.stat_result


def _compute_sha256(data: bytes) -> str:
    return _SHA256_PREFIX + hashlib.sha256(data).hexdigest()


def _is_valid_sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith(_SHA256_PREFIX):
        return False
    hex_digest = value[len(_SHA256_PREFIX):]
    if len(hex_digest) != _SHA256_HEX_LEN:
        return False
    try:
        bytes.fromhex(hex_digest)
    except ValueError:
        return False
    return True


def _parse_expected_sha256(expected_sha256: str) -> str:
    if not _is_valid_sha256(expected_sha256):
        raise SupervisorIntegrationError(
            f"expected_sha256 must have the form 'sha256:<{_SHA256_HEX_LEN} hex>'; got {expected_sha256!r}"
        )
    return expected_sha256.lower()


def _resolve_target(resolved_root: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` under ``resolved_root`` and enforce containment.

    Rejects absolute paths, any ``..`` component and any resolution that
    ends up outside ``resolved_root`` (including intermediate-directory
    symlink escapes).
    """
    if os.path.isabs(relative_path):
        raise PathEscapeError(f"absolute paths are rejected: {relative_path!r}")
    if ".." in Path(relative_path).parts:
        raise PathEscapeError(f"path traversal ('..') is rejected: {relative_path!r}")
    raw = resolved_root / relative_path
    try:
        parent_resolved = raw.parent.resolve()
    except OSError as exc:
        raise PathEscapeError(f"cannot resolve parent of {relative_path!r}: {exc}") from exc
    target = parent_resolved / raw.name
    resolved_target = target.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise PathEscapeError(f"path escapes project root: {relative_path!r}")
    return target


def _open_verified_target(target: Path) -> _TargetHandle:
    """Open the target with O_NOFOLLOW and verify it is a regular single-link file.

    Raises a matching ``SupervisorIntegrationError`` subclass and never
    returns a descriptor for an unverified target.
    """
    try:
        lst = target.lstat()
    except FileNotFoundError:
        raise TargetMissingError(f"target does not exist: {target}") from None
    except OSError as exc:
        raise SupervisorIntegrationError(f"cannot stat {target}: {exc}") from exc
    if stat.S_ISLNK(lst.st_mode):
        raise SymlinkRejectedError(
            f"target is a symlink; symlink targets are unsupported: {target}"
        )
    if _NOFOLLOW is None:
        raise SupervisorIntegrationError(
            "O_NOFOLLOW is unavailable on this platform; refusing to open the target"
        )
    try:
        fd = os.open(target, os.O_RDWR | _NOFOLLOW)
    except FileNotFoundError:
        raise TargetMissingError(f"target does not exist: {target}") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SymlinkRejectedError(f"target resolved to a symlink: {target}") from exc
        raise UnsupportedTargetError(f"cannot open target for read/write: {target}: {exc}") from exc
    try:
        fst = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    if not stat.S_ISREG(fst.st_mode):
        os.close(fd)
        raise UnsupportedTargetError(f"target is not a regular file: {target}")
    if fst.st_nlink != 1:
        os.close(fd)
        raise UnsupportedTargetError(
            f"target has {fst.st_nlink} hard links; only single-link files are supported: {target}"
        )
    if (lst.st_dev, lst.st_ino) != (fst.st_dev, fst.st_ino):
        os.close(fd)
        raise SupervisorIntegrationError(
            f"target path changed between stat and open; refusing: {target}"
        )
    return _TargetHandle(target=target, fd=fd, lstat=lst, fstat=fst)


def _read_all_fd(fd: int) -> bytes:
    """Read every byte of the open file from the current position (rewound to 0)."""
    chunks = []
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, _READ_CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(fd: int, data: bytes) -> None:
    """Full write loop at offset 0, then ftruncate and fsync."""
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    total = 0
    while total < len(view):
        written = os.write(fd, view[total:])
        if written == 0:
            raise OSError(errno.EIO, f"short write after {total}/{len(view)} bytes")
        total += written
    os.ftruncate(fd, len(view))
    os.fsync(fd)


def _write_new_bytes(fd: int, data: bytes) -> None:
    """Forward mutation path; kept as a seam so tests can inject a partial-write failure."""
    _write_all(fd, data)


def _restore_fd(fd: int, data: bytes) -> None:
    """Rollback path; restores original bytes through the same pinned fd."""
    _write_all(fd, data)


def _read_and_verify_hash(fd: int, expected_hash: str, target: Path) -> tuple[bytes, str]:
    original = _read_all_fd(fd)
    original_hash = _compute_sha256(original)
    if original_hash != expected_hash:
        raise HashMismatchError(
            f"expected {expected_hash} for {target}, found {original_hash}; refusing to write"
        )
    return original, original_hash


def _verify_fd_content(fd: int, expected_hash: str, label: str) -> None:
    """fsync then re-read and hash the open file through the pinned descriptor.

    Used after a restore/rollback write so the journal is only cleaned up
    once the bytes on disk are proven to match ``expected_hash``.
    """
    os.fsync(fd)
    actual_hash = _compute_sha256(_read_all_fd(fd))
    if actual_hash != expected_hash:
        raise HashMismatchError(
            f"post-write verification failed for {label}: "
            f"expected {expected_hash}, found {actual_hash}"
        )


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _journal_paths(journal_root: Path, resolved_root: Path, relative_path: str) -> tuple[Path, Path, Path]:
    digest = hashlib.sha256(f"{resolved_root}\0{relative_path}".encode()).hexdigest()
    return (
        journal_root / f"{digest}.json",
        journal_root / f"{digest}.bak",
        journal_root / f"{digest}.lock",
    )


@contextmanager
def _target_lock(journal_root: Path, resolved_root: Path, relative_path: str) -> Iterator[Path]:
    """Per-target advisory flock backed by a private lock file under ``journal_root``."""
    journal_root.mkdir(parents=True, exist_ok=True)
    os.chmod(journal_root, 0o700)
    _, _, lock_path = _journal_paths(journal_root, resolved_root, relative_path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield lock_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_backup(backup_path: Path, original_bytes: bytes) -> None:
    try:
        fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise JournalPendingError(f"pending journal backup already exists: {backup_path}") from None
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, original_bytes)
    finally:
        os.close(fd)


def _write_journal(journal_path: Path, meta: dict) -> None:
    payload = json.dumps(meta, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    try:
        fd = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise JournalPendingError(f"pending recovery journal already exists: {journal_path}") from None
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, payload)
    finally:
        os.close(fd)


def _prepare_journal(
    journal_root: Path,
    resolved_root: Path,
    relative_path: str,
    target: Path,
    fstat: os.stat_result,
    original_bytes: bytes,
    new_hash: str,
) -> tuple[Path, Path, dict]:
    """Persist the byte-exact backup and journal metadata before any mutation.

    Order: fsync backup -> fsync journal metadata -> fsync journal directory.
    """
    journal_root.mkdir(parents=True, exist_ok=True)
    os.chmod(journal_root, 0o700)
    journal_path, backup_path, _ = _journal_paths(journal_root, resolved_root, relative_path)
    meta = {
        "version": _JOURNAL_VERSION,
        "project_root": str(resolved_root),
        "relative_path": relative_path,
        "target_path": str(target),
        "dev": fstat.st_dev,
        "ino": fstat.st_ino,
        "original_hash": _compute_sha256(original_bytes),
        "new_hash": new_hash,
        "backup": backup_path.name,
    }
    _write_backup(backup_path, original_bytes)
    try:
        _write_journal(journal_path, meta)
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    _fsync_dir(journal_root)
    return journal_path, backup_path, meta


def _cleanup_journal(journal_path: Path, backup_path: Path, journal_root: Path) -> None:
    journal_path.unlink(missing_ok=True)
    backup_path.unlink(missing_ok=True)
    _fsync_dir(journal_root)


def _rollback(
    fd: int,
    original: bytes,
    journal_path: Path,
    backup_path: Path,
    journal_root: Path,
    target: Path,
    cause: BaseException,
) -> None:
    """Restore original bytes through the pinned fd; retain the journal if that fails.

    After a successful restore the bytes are fsynced, re-read and hashed
    through the same pinned fd and must equal the original hash before the
    journal is cleaned up; any mismatch raises ``RollbackFailedError`` and
    retains the journal for manual intervention.
    """
    try:
        _restore_fd(fd, original)
    except BaseException:
        logger.exception(
            "rollback failed for %s; recovery journal retained at %s", journal_path, journal_path
        )
        raise RollbackFailedError(
            f"write failed and restoring the original bytes also failed; "
            f"recovery journal retained for manual intervention: {journal_path}"
        ) from cause
    try:
        _verify_fd_content(fd, _compute_sha256(original), str(target))
    except (HashMismatchError, OSError) as exc:
        logger.exception(
            "post-rollback verification failed for %s; recovery journal retained at %s",
            journal_path,
            journal_path,
        )
        raise RollbackFailedError(
            f"write failed and post-rollback verification of the restored bytes failed; "
            f"recovery journal retained for manual intervention: {journal_path}: {exc}"
        ) from cause
    _cleanup_journal(journal_path, backup_path, journal_root)


def integrate_file(
    project_root: Path | str,
    relative_path: str | Path,
    expected_sha256: str,
    new_bytes: bytes | str,
    journal_root: Path | str,
) -> IntegrateResult:
    """Replace the content of ONE existing regular file with ``new_bytes``.

    ``expected_sha256`` is mandatory and must be ``'sha256:<hex>'``; the
    original content is read through the pinned descriptor and verified
    before anything is written. A per-target journal/backup is persisted
    (and fsynced) before mutation, and every precondition is re-checked
    under the per-target lock.
    """
    project_root = Path(project_root)
    journal_root = Path(journal_root)
    relative_path = os.fspath(relative_path).strip()
    if not relative_path:
        raise PathEscapeError("relative_path must not be empty")
    if os.path.isabs(relative_path):
        raise PathEscapeError(f"absolute paths are rejected: {relative_path!r}")
    if isinstance(new_bytes, str):
        new_bytes = new_bytes.encode("utf-8")
    if not isinstance(new_bytes, (bytes, bytearray, memoryview)):
        raise TypeError("new_bytes must be bytes, bytearray or str")
    new_bytes = bytes(new_bytes)
    expected_hash = _parse_expected_sha256(expected_sha256)
    resolved_root = project_root.resolve()

    # Fail-fast precondition validation before taking the per-target lock.
    target = _resolve_target(resolved_root, relative_path)
    preflight = _open_verified_target(target)
    try:
        _read_and_verify_hash(preflight.fd, expected_hash, target)
    finally:
        os.close(preflight.fd)

    with _target_lock(journal_root, resolved_root, relative_path):
        # Repeat lstat/open/hash under the lock: waiting on the flock must
        # not stale the preconditions (path, symlink state, inode, content).
        target = _resolve_target(resolved_root, relative_path)
        journal_path, backup_path, _ = _journal_paths(journal_root, resolved_root, relative_path)
        if journal_path.exists():
            raise JournalPendingError(
                f"pending recovery journal exists for {relative_path!r}: {journal_path}; "
                "run recover_pending() before integrating again"
            )
        handle = _open_verified_target(target)
        try:
            original, original_hash = _read_and_verify_hash(handle.fd, expected_hash, target)
            new_hash = _compute_sha256(new_bytes)
            _prepare_journal(
                journal_root, resolved_root, relative_path, target, handle.fstat, original, new_hash
            )
            try:
                _write_new_bytes(handle.fd, new_bytes)
            except BaseException as exc:
                _rollback(handle.fd, original, journal_path, backup_path, journal_root, target, exc)
                raise
            _cleanup_journal(journal_path, backup_path, journal_root)
        finally:
            os.close(handle.fd)

    return IntegrateResult(
        relative_path=relative_path,
        target_path=str(target),
        original_hash=original_hash,
        new_hash=new_hash,
    )


def _require_str_field(meta: dict, key: str) -> str:
    value = meta.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SupervisorIntegrationError(f"journal {key!r} must be a non-empty string")
    return value.strip()


def _require_int_field(meta: dict, key: str) -> int:
    value = meta.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SupervisorIntegrationError(
            f"journal {key!r} must be an integer; got {type(value).__name__}"
        )
    return value


def _require_hash_field(meta: dict, key: str) -> str:
    value = meta.get(key)
    if not _is_valid_sha256(value):
        raise SupervisorIntegrationError(
            f"journal {key!r} must be a valid 'sha256:<{_SHA256_HEX_LEN} hex>' string"
        )
    return value.lower()


def _validate_journal_meta(journal_path: Path, meta: object) -> dict:
    """Type-validate every journal field consumed by recovery.

    Returns a normalized copy of ``meta``. Raises ``SupervisorIntegrationError``
    for malformed-but-parseable values so the journal fails closed and is
    retained instead of crashing the whole ``recover_pending`` sweep.
    """
    if not isinstance(meta, dict):
        raise SupervisorIntegrationError("journal payload must be a JSON object")
    version = meta.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SupervisorIntegrationError(
            f"journal version must be an integer; got {type(version).__name__}"
        )
    if version != _JOURNAL_VERSION:
        raise SupervisorIntegrationError(
            f"unsupported journal version {version!r}; expected {_JOURNAL_VERSION}"
        )
    validated = {
        "version": version,
        "project_root": _require_str_field(meta, "project_root"),
        "relative_path": _require_str_field(meta, "relative_path"),
        "target_path": _require_str_field(meta, "target_path"),
        "dev": _require_int_field(meta, "dev"),
        "ino": _require_int_field(meta, "ino"),
        "original_hash": _require_hash_field(meta, "original_hash"),
        "new_hash": _require_hash_field(meta, "new_hash"),
    }
    backup = meta.get("backup")
    if not isinstance(backup, str) or not backup:
        raise SupervisorIntegrationError("journal 'backup' must be a non-empty string")
    if Path(backup).name != backup:
        raise SupervisorIntegrationError("journal backup must be a plain filename")
    expected_backup = journal_path.with_suffix(".bak").name
    if backup != expected_backup:
        raise SupervisorIntegrationError(
            f"journal backup {backup!r} does not match expected journal backup {expected_backup!r}"
        )
    validated["backup"] = backup
    return validated


def _recover_one(
    resolved_root: Path, journal_root: Path, journal_path: Path, meta: dict
) -> RecoveryResult:
    relative_path = meta["relative_path"]
    backup_name = meta["backup"]
    try:
        target = _resolve_target(resolved_root, relative_path)
    except SupervisorIntegrationError as exc:
        return RecoveryResult(relative_path, "error", True, str(exc))
    try:
        if Path(meta["target_path"]).resolve() != target:
            return RecoveryResult(
                relative_path, "error", True, "journal target_path does not match resolved target"
            )
    except OSError as exc:
        return RecoveryResult(
            relative_path, "error", True, f"cannot resolve journal target_path: {exc}"
        )
    expected_dev = meta["dev"]
    expected_ino = meta["ino"]
    backup_path = journal_root / backup_name
    try:
        handle = _open_verified_target(target)
    except SupervisorIntegrationError as exc:
        return RecoveryResult(relative_path, "error", True, str(exc))
    try:
        if (handle.fstat.st_dev, handle.fstat.st_ino) != (expected_dev, expected_ino):
            return RecoveryResult(
                relative_path, "error", True, "target inode changed; refusing automatic recovery"
            )
        current_hash = _compute_sha256(_read_all_fd(handle.fd))
        original_hash = meta["original_hash"]
        new_hash = meta["new_hash"]
        if current_hash == new_hash:
            _cleanup_journal(journal_path, backup_path, journal_root)
            return RecoveryResult(relative_path, "completed", False)
        if current_hash == original_hash:
            _cleanup_journal(journal_path, backup_path, journal_root)
            return RecoveryResult(relative_path, "unchanged", False)
        try:
            backup_bytes = backup_path.read_bytes()
        except OSError as exc:
            return RecoveryResult(relative_path, "error", True, f"cannot read backup: {exc}")
        if _compute_sha256(backup_bytes) != original_hash:
            return RecoveryResult(
                relative_path, "error", True, "backup hash does not match journal original_hash"
            )
        try:
            _write_all(handle.fd, backup_bytes)
        except OSError as exc:
            return RecoveryResult(relative_path, "error", True, f"cannot restore backup: {exc}")
        try:
            _verify_fd_content(handle.fd, original_hash, str(target))
        except (HashMismatchError, OSError) as exc:
            return RecoveryResult(
                relative_path,
                "error",
                True,
                f"post-restore verification failed; journal retained: {exc}",
            )
        _cleanup_journal(journal_path, backup_path, journal_root)
        return RecoveryResult(relative_path, "restored", False)
    finally:
        os.close(handle.fd)


def recover_pending(project_root: Path | str, journal_root: Path | str) -> list[RecoveryResult]:
    """Reconcile every pending journal under ``journal_root`` for this project.

    Each journal is parsed and fully type-validated on its own: a malformed
    or unsupported-version journal fails closed and is retained without
    aborting the sweep, and journals that do not belong to this project are
    skipped. Failed-closed journals (bad/unsupported metadata, target
    inode/path changed or target unsafe, post-restore verification failure)
    are retained for manual intervention and reported with status ``error``.
    """
    resolved_root = Path(project_root).resolve()
    journal_root = Path(journal_root)
    if not journal_root.is_dir():
        return []
    results: list[RecoveryResult] = []
    for journal_path in sorted(journal_root.glob("*.json")):
        try:
            meta = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("unreadable pending journal %s; retained for manual intervention", journal_path)
            results.append(
                RecoveryResult(
                    relative_path=journal_path.name,
                    status="error",
                    journal_retained=True,
                    error=f"unreadable journal: {exc}",
                )
            )
            continue
        relative_path = journal_path.name
        try:
            meta = _validate_journal_meta(journal_path, meta)
            relative_path = meta["relative_path"]
            if Path(meta["project_root"]).resolve() != resolved_root:
                continue
            with _target_lock(journal_root, resolved_root, relative_path):
                results.append(_recover_one(resolved_root, journal_root, journal_path, meta))
        except SupervisorIntegrationError as exc:
            logger.warning("recovery failed closed for %s: %s", journal_path, exc)
            results.append(
                RecoveryResult(
                    relative_path=relative_path, status="error", journal_retained=True, error=str(exc)
                )
            )
        except (OSError, RuntimeError) as exc:
            logger.warning("recovery failed for %s: %s", journal_path, exc)
            results.append(
                RecoveryResult(
                    relative_path=relative_path, status="error", journal_retained=True, error=str(exc)
                )
            )
    return results


__all__ = [
    "HashMismatchError",
    "IntegrateResult",
    "JournalPendingError",
    "PathEscapeError",
    "RecoveryResult",
    "RollbackFailedError",
    "SupervisorIntegrationError",
    "SymlinkRejectedError",
    "TargetMissingError",
    "UnsupportedTargetError",
    "integrate_file",
    "recover_pending",
]
