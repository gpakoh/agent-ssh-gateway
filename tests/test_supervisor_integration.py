"""Tests for the supervisor integration primitive.

Covers containment/escape rejection, mandatory hash verification through
the pinned fd, symlink/hardlink rejection, per-target locking, journal
persistence (0700/0600), partial-write rollback (including post-rollback
verification), and the deterministic ``recover_pending`` state machine
(completed / unchanged / restored / failed-closed), including per-journal
type validation (malformed values and unsupported versions must not abort
the sweep) and post-restore hash verification. POSIX-only (fcntl +
O_NOFOLLOW), matching ``supervisor_integration.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

if "examples/mcp_server" not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server"))

_HAS_POSIX = True
try:
    import fcntl  # noqa: F401
except ImportError:
    _HAS_POSIX = False

pytestmark = pytest.mark.skipif(
    not _HAS_POSIX,
    reason="supervisor_integration requires fcntl/O_NOFOLLOW (POSIX-only)",
)

from examples.mcp_server import supervisor_integration as si  # noqa: E402

ORIGINAL = "line1\nline2\n"
NEW = "new content\n"
NEW2 = "even newer content\n"
ORIGINAL_BYTES = ORIGINAL.encode("utf-8")
NEW_BYTES = NEW.encode("utf-8")
NEW2_BYTES = NEW2.encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _prepare(root: Path, text: str = ORIGINAL) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    target = root / "f.txt"
    target.write_text(text, encoding="utf-8")
    return target


class TestIntegrateSuccess:
    def test_writes_new_content_and_preserves_mode_owner(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        os.chmod(target, 0o640)
        before = target.stat()
        journal_root = tmp_path / "journal"

        result = si.integrate_file(root, "f.txt", _sha256(ORIGINAL_BYTES), NEW, journal_root)

        assert target.read_text(encoding="utf-8") == NEW
        assert result.relative_path == "f.txt"
        assert result.original_hash == _sha256(ORIGINAL_BYTES)
        assert result.new_hash == _sha256(NEW_BYTES)
        after = target.stat()
        assert stat.S_IMODE(after.st_mode) == 0o640
        assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
        assert not list(journal_root.glob("*.json"))
        assert not list(journal_root.glob("*.bak"))

    def test_accepts_str_content(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        si.integrate_file(root, "f.txt", _sha256(ORIGINAL_BYTES), NEW, tmp_path / "journal")
        assert target.read_text(encoding="utf-8") == NEW

    def test_when_parent_dir_not_writable(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("permission checks are meaningless when running as root")
        root = tmp_path / "proj"
        (root / "sub").mkdir(parents=True)
        target = root / "sub" / "cfg.txt"
        target.write_text(ORIGINAL, encoding="utf-8")
        os.chmod(root / "sub", 0o500)
        try:
            result = si.integrate_file(
                root, "sub/cfg.txt", _sha256(ORIGINAL_BYTES), NEW, tmp_path / "journal"
            )
            assert target.read_text(encoding="utf-8") == NEW
            assert result.new_hash == _sha256(NEW_BYTES)
        finally:
            os.chmod(root / "sub", 0o700)

    def test_idempotent_successive_calls(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"

        r1 = si.integrate_file(root, "f.txt", _sha256(ORIGINAL_BYTES), NEW, journal_root)
        r2 = si.integrate_file(root, "f.txt", _sha256(NEW_BYTES), NEW2, journal_root)

        assert target.read_text(encoding="utf-8") == NEW2
        assert r1.new_hash == _sha256(NEW_BYTES)
        assert r2.new_hash == _sha256(NEW2_BYTES)
        assert not list(journal_root.glob("*.json"))


class TestPreconditionRejection:
    def test_hash_mismatch_no_write(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"

        with pytest.raises(si.HashMismatchError):
            si.integrate_file(root, "f.txt", _sha256(b"wrong bytes"), NEW, journal_root)

        assert target.read_bytes() == ORIGINAL_BYTES
        assert not list(journal_root.glob("*.json"))
        assert not list(journal_root.glob("*.bak"))

    def test_missing_target_rejected(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        with pytest.raises(si.TargetMissingError):
            si.integrate_file(root, "nope.txt", _sha256(ORIGINAL_BYTES), NEW, tmp_path / "journal")

    def test_directory_target_rejected(self, tmp_path):
        root = tmp_path / "proj"
        (root / "d").mkdir(parents=True)
        with pytest.raises(si.UnsupportedTargetError):
            si.integrate_file(root, "d", _sha256(ORIGINAL_BYTES), NEW, tmp_path / "journal")

    def test_empty_relative_path_rejected(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        with pytest.raises(si.PathEscapeError):
            si.integrate_file(root, "", _sha256(ORIGINAL_BYTES), NEW, tmp_path / "journal")

    @pytest.mark.parametrize(
        "rel",
        ["../x", "../../etc/passwd", "a/../../x", "sub/..", "..", "/etc/passwd"],
    )
    def test_path_escape_rejected(self, tmp_path, rel):
        root = tmp_path / "proj"
        root.mkdir()
        with pytest.raises(si.PathEscapeError):
            si.integrate_file(root, rel, _sha256(ORIGINAL_BYTES), NEW, tmp_path / "journal")

    def test_symlink_target_rejected(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        real = root / "actual.txt"
        real.write_text(ORIGINAL, encoding="utf-8")
        link = root / "f.txt"
        link.symlink_to(real.name)
        with pytest.raises(si.SymlinkRejectedError):
            si.integrate_file(root, "f.txt", _sha256(ORIGINAL_BYTES), NEW, tmp_path / "journal")

    def test_symlink_escaping_root_rejected(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        linkdir = root / "linkdir"
        linkdir.symlink_to(outside, target_is_directory=True)
        with pytest.raises(si.PathEscapeError):
            si.integrate_file(
                root, "linkdir/secret.txt", _sha256(b"secret"), NEW, tmp_path / "journal"
            )

    def test_hardlink_target_rejected(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        os.link(target, root / "alias.txt")
        with pytest.raises(si.UnsupportedTargetError):
            si.integrate_file(root, "f.txt", _sha256(ORIGINAL_BYTES), NEW, tmp_path / "journal")
        assert target.read_bytes() == ORIGINAL_BYTES

    @pytest.mark.parametrize("bad", ["md5:abcd", "sha256:xyz", "sha256:abc", "sha256:", ""])
    def test_expected_hash_format_validated(self, tmp_path, bad):
        root = tmp_path / "proj"
        _prepare(root)
        with pytest.raises(si.SupervisorIntegrationError):
            si.integrate_file(root, "f.txt", bad, NEW, tmp_path / "journal")


class TestLockingAndJournal:
    def test_lock_blocks_concurrent_holder(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        journal_root = tmp_path / "journal"
        resolved_root = root.resolve()
        with si._target_lock(journal_root, resolved_root, "f.txt"):
            _, _, lock_path = si._journal_paths(journal_root, resolved_root, "f.txt")
            fd = os.open(lock_path, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_journal_dir_and_files_modes(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"

        journal_path, backup_path, _ = si._prepare_journal(
            journal_root,
            root.resolve(),
            "f.txt",
            target,
            target.stat(),
            ORIGINAL_BYTES,
            _sha256(NEW_BYTES),
        )
        try:
            assert stat.S_IMODE(journal_root.stat().st_mode) == 0o700
            for p in (journal_path, backup_path):
                assert stat.S_IMODE(p.stat().st_mode) == 0o600
        finally:
            si._cleanup_journal(journal_path, backup_path, journal_root)

    def test_journal_backup_is_byte_exact(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"

        journal_path, backup_path, meta = si._prepare_journal(
            journal_root,
            root.resolve(),
            "f.txt",
            target,
            target.stat(),
            ORIGINAL_BYTES,
            _sha256(NEW_BYTES),
        )
        try:
            assert backup_path.read_bytes() == ORIGINAL_BYTES
            assert meta["original_hash"] == _sha256(ORIGINAL_BYTES)
            assert meta["new_hash"] == _sha256(NEW_BYTES)
        finally:
            si._cleanup_journal(journal_path, backup_path, journal_root)

    def test_pending_journal_blocks_integrate(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"

        journal_path, backup_path, _ = si._prepare_journal(
            journal_root,
            root.resolve(),
            "f.txt",
            target,
            target.stat(),
            ORIGINAL_BYTES,
            _sha256(NEW_BYTES),
        )
        try:
            with pytest.raises(si.JournalPendingError):
                si.integrate_file(root, "f.txt", _sha256(ORIGINAL_BYTES), NEW, journal_root)
            assert target.read_bytes() == ORIGINAL_BYTES
        finally:
            si._cleanup_journal(journal_path, backup_path, journal_root)


class TestRollback:
    def test_partial_write_exception_rolls_back(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"

        def _simulate_partial(fd: int, data: bytes) -> None:
            os.lseek(fd, 0, os.SEEK_SET)
            n = os.write(fd, b"CORRUPTED-")
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            assert os.read(fd, n) == b"CORRUPTED-"
            raise OSError("simulated partial-write failure")

        monkeypatch.setattr(si, "_write_new_bytes", _simulate_partial)
        with pytest.raises(OSError, match="simulated partial-write failure"):
            si.integrate_file(root, "f.txt", _sha256(ORIGINAL_BYTES), NEW, journal_root)

        assert target.read_bytes() == ORIGINAL_BYTES
        assert not list(journal_root.glob("*.json"))
        assert not list(journal_root.glob("*.bak"))

    def test_rollback_failure_raises_severe_and_retains_journal(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"

        def _simulate_partial(fd: int, data: bytes) -> None:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"CORRUPTED-")
            os.fsync(fd)
            raise OSError("simulated write failure")

        def _simulate_restore_failure(fd: int, data: bytes) -> None:
            raise OSError("simulated rollback failure")

        monkeypatch.setattr(si, "_write_new_bytes", _simulate_partial)
        monkeypatch.setattr(si, "_restore_fd", _simulate_restore_failure)
        with pytest.raises(si.RollbackFailedError):
            si.integrate_file(root, "f.txt", _sha256(ORIGINAL_BYTES), NEW, journal_root)

        assert target.read_bytes().startswith(b"CORRUPTED-")
        assert list(journal_root.glob("*.json"))
        assert list(journal_root.glob("*.bak"))

    def test_post_rollback_verification_failure_retains_journal(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"

        def _simulate_partial(fd: int, data: bytes) -> None:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"CORRUPTED-")
            os.fsync(fd)
            raise OSError("simulated write failure")

        def _simulate_bad_verify(fd: int, expected_hash: str, label: str) -> None:
            raise si.HashMismatchError("simulated post-rollback hash mismatch")

        monkeypatch.setattr(si, "_write_new_bytes", _simulate_partial)
        monkeypatch.setattr(si, "_verify_fd_content", _simulate_bad_verify)
        with pytest.raises(si.RollbackFailedError):
            si.integrate_file(root, "f.txt", _sha256(ORIGINAL_BYTES), NEW, journal_root)

        assert target.read_bytes() == ORIGINAL_BYTES
        assert list(journal_root.glob("*.json"))
        assert list(journal_root.glob("*.bak"))


class TestRecoverPending:
    def _pending_journal(self, root: Path, journal_root: Path) -> tuple[Path, Path]:
        target = root / "f.txt"
        return si._prepare_journal(
            journal_root,
            root.resolve(),
            "f.txt",
            target,
            target.stat(),
            ORIGINAL_BYTES,
            _sha256(NEW_BYTES),
        )[:2]

    def test_restores_partial_state(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"
        journal_path, backup_path = self._pending_journal(root, journal_root)

        fd = os.open(target, os.O_RDWR)
        try:
            os.write(fd, NEW[:7].encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        assert target.read_text(encoding="utf-8") != ORIGINAL
        assert target.read_text(encoding="utf-8") != NEW

        results = si.recover_pending(root, journal_root)

        assert len(results) == 1
        assert results[0].status == "restored"
        assert results[0].journal_retained is False
        assert target.read_text(encoding="utf-8") == ORIGINAL
        assert not journal_path.exists()
        assert not backup_path.exists()

    def test_cleans_when_integration_completed(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"
        journal_path, backup_path = self._pending_journal(root, journal_root)
        target.write_text(NEW, encoding="utf-8")

        results = si.recover_pending(root, journal_root)

        assert results[0].status == "completed"
        assert results[0].journal_retained is False
        assert target.read_text(encoding="utf-8") == NEW
        assert not journal_path.exists()
        assert not backup_path.exists()

    def test_cleans_when_untouched(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"
        journal_path, backup_path = self._pending_journal(root, journal_root)

        results = si.recover_pending(root, journal_root)

        assert results[0].status == "unchanged"
        assert results[0].journal_retained is False
        assert target.read_text(encoding="utf-8") == ORIGINAL
        assert not journal_path.exists()
        assert not backup_path.exists()

    def test_fails_closed_on_inode_change(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"
        journal_path, backup_path = self._pending_journal(root, journal_root)
        # Keep the original inode alive via an open fd so unlink+recreate
        # is guaranteed to allocate a NEW inode (otherwise tmpfs reuses the
        # freed inode number and the dev/ino guard cannot observe a change).
        held = os.open(target, os.O_RDONLY)
        try:
            target.unlink()
            target.write_text("someone else", encoding="utf-8")
            assert target.stat().st_ino != os.fstat(held).st_ino
        finally:
            os.close(held)

        results = si.recover_pending(root, journal_root)

        assert results[0].status == "error"
        assert results[0].journal_retained is True
        assert target.read_text(encoding="utf-8") == "someone else"
        assert journal_path.exists()
        assert backup_path.exists()

    def test_fails_closed_on_symlink_swap(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"
        journal_path, _ = self._pending_journal(root, journal_root)
        target.unlink()
        target.symlink_to(tmp_path / "elsewhere.txt")

        results = si.recover_pending(root, journal_root)

        assert results[0].status == "error"
        assert results[0].journal_retained is True
        assert journal_path.exists()

    def test_no_journal_root_returns_empty(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        assert si.recover_pending(root, tmp_path / "missing-journal") == []

    def test_corrupt_journal_fails_closed(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        journal_root = tmp_path / "journal"
        journal_root.mkdir(mode=0o700)
        corrupt = journal_root / "zz.json"
        corrupt.write_text("{ not json", encoding="utf-8")

        results = si.recover_pending(root, journal_root)

        assert results[0].status == "error"
        assert results[0].journal_retained is True
        assert corrupt.exists()

    def _valid_meta(self, root: Path, target: Path) -> dict:
        st = target.stat()
        return {
            "version": si._JOURNAL_VERSION,
            "project_root": str(root.resolve()),
            "relative_path": "f.txt",
            "target_path": str(target),
            "dev": st.st_dev,
            "ino": st.st_ino,
            "original_hash": _sha256(ORIGINAL_BYTES),
            "new_hash": _sha256(NEW_BYTES),
            "backup": "0000000000.bak",
        }

    def _manual_journal(self, journal_root: Path, name: str, meta: dict) -> Path:
        journal_root.mkdir(mode=0o700, exist_ok=True)
        path = journal_root / name
        path.write_text(json.dumps(meta), encoding="utf-8")
        return path

    @pytest.mark.parametrize(
        "field,value",
        [
            ("version", "1"),
            ("version", True),
            ("version", 1.5),
            ("project_root", 123),
            ("project_root", None),
            ("relative_path", 123),
            ("relative_path", ["f.txt"]),
            ("target_path", 99),
            ("dev", "not-an-int"),
            ("dev", True),
            ("ino", 12.5),
            ("original_hash", 42),
            ("original_hash", "sha256:not-hex"),
            ("new_hash", None),
            ("backup", "../escape.bak"),
            ("backup", 7),
            ("backup", "different-name.bak"),
        ],
    )
    def test_malformed_value_does_not_abort_sweep(self, tmp_path, field, value):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"
        meta = self._valid_meta(root, target)
        meta[field] = value
        bad = self._manual_journal(journal_root, "0000000000.json", meta)
        valid_journal, valid_backup = self._pending_journal(root, journal_root)

        results = si.recover_pending(root, journal_root)

        assert len(results) == 2
        assert results[0].status == "error"
        assert results[0].journal_retained is True
        assert bad.exists()
        assert results[1].status == "unchanged"
        assert results[1].journal_retained is False
        assert not valid_journal.exists()
        assert not valid_backup.exists()

    def test_unsupported_journal_version_retained(self, tmp_path):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"
        meta = self._valid_meta(root, target)
        meta["version"] = 2
        bad = self._manual_journal(journal_root, "0000000000.json", meta)
        valid_journal, _ = self._pending_journal(root, journal_root)

        results = si.recover_pending(root, journal_root)

        assert results[0].status == "error"
        assert results[0].journal_retained is True
        assert "version" in results[0].error
        assert bad.exists()
        assert results[1].status == "unchanged"
        assert results[1].journal_retained is False
        assert not valid_journal.exists()

    def test_post_restore_verification_failure_retains_journal(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"
        journal_path, backup_path = self._pending_journal(root, journal_root)

        fd = os.open(target, os.O_RDWR)
        try:
            os.write(fd, NEW[:7].encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        assert target.read_text(encoding="utf-8") not in (ORIGINAL, NEW)

        def _bad_verify(fd: int, expected_hash: str, label: str) -> None:
            raise si.HashMismatchError("simulated post-restore hash mismatch")

        monkeypatch.setattr(si, "_verify_fd_content", _bad_verify)
        results = si.recover_pending(root, journal_root)

        assert results[0].status == "error"
        assert results[0].journal_retained is True
        assert "verification" in results[0].error
        assert journal_path.exists()
        assert backup_path.exists()

    def test_post_restore_oserror_does_not_abort_sweep(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        target = _prepare(root)
        journal_root = tmp_path / "journal"
        journal_path, backup_path = self._pending_journal(root, journal_root)
        fd = os.open(target, os.O_RDWR)
        try:
            os.write(fd, NEW[:7].encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

        def _bad_verify(fd: int, expected_hash: str, label: str) -> None:
            raise OSError("simulated fsync failure")

        monkeypatch.setattr(si, "_verify_fd_content", _bad_verify)
        results = si.recover_pending(root, journal_root)

        assert results[0].status == "error"
        assert results[0].journal_retained is True
        assert journal_path.exists()
        assert backup_path.exists()
