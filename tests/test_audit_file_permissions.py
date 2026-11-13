"""Legacy Gateway security audit: strict file modes and safe logrotate reopen."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from app.security import AuditLogger, _SecureAuditFileHandler


@pytest.fixture
def isolated_audit():
    """Test the real singleton logger without altering another test's handlers."""
    logger = logging.getLogger("audit")
    previous = list(logger.handlers)
    level, propagate = logger.level, logger.propagate
    for handler in previous:
        logger.removeHandler(handler)
    logger.propagate = False
    try:
        yield logger
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        for handler in previous:
            logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_new_audit_directory_is_0700_even_under_permissive_umask(
    tmp_path, isolated_audit,
):
    path = tmp_path / "new" / "audit.log"
    saved = os.umask(0o000)
    try:
        AuditLogger(str(path))
    finally:
        os.umask(saved)
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600


def test_all_new_audit_ancestors_are_0700_with_permissive_umask(
    tmp_path, isolated_audit,
):
    path = tmp_path / "audit-root" / "tenant" / "journal" / "audit.log"
    saved = os.umask(0o000)
    try:
        AuditLogger(str(path))
    finally:
        os.umask(saved)
    for directory in [tmp_path / "audit-root", tmp_path / "audit-root" / "tenant", path.parent]:
        assert _mode(directory) == 0o700
    assert _mode(path) == 0o600


def test_existing_shared_ancestor_is_not_chmodded_when_nesting_audit_dir(
    tmp_path, isolated_audit,
):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    path = shared / "new" / "nested" / "audit.log"
    saved = os.umask(0o000)
    try:
        AuditLogger(str(path))
    finally:
        os.umask(saved)
    assert _mode(shared) == 0o755
    assert _mode(shared / "new") == 0o700
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600


def test_intermediate_audit_directory_symlink_is_denied_without_writes(
    tmp_path, isolated_audit,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        AuditLogger(str(link / "nested" / "audit.log"))
    assert not (outside / "nested").exists()


def test_existing_audit_directory_permissions_not_changed(tmp_path, isolated_audit):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)  # Keep the pre-existing directory mode independent of CI umask.
    AuditLogger(str(shared / "audit.log"))
    assert _mode(shared) == 0o755


def test_new_audit_file_is_0600_even_if_umask_is_world_writable(tmp_path, isolated_audit):
    path = tmp_path / "audit.log"
    saved = os.umask(0o000)
    try:
        audit = AuditLogger(str(path))
    finally:
        os.umask(saved)
    assert _mode(path) == 0o600
    audit.log_auth("user", True, "127.0.0.1")
    assert "AUTH | user=user" in path.read_text(encoding="utf-8")


def test_existing_insecure_audit_inode_repaired_in_place_without_data_loss(
    tmp_path, isolated_audit,
):
    path = tmp_path / "audit.log"
    existing_bytes = b"historical journal bytes must survive\\n"
    path.write_bytes(existing_bytes)
    inode = path.stat().st_ino
    path.chmod(0o606)
    audit = AuditLogger(str(path))
    assert _mode(path) == 0o600
    assert path.stat().st_ino == inode
    assert path.read_bytes() == existing_bytes
    audit.log_command("session", "hostname", "127.0.0.1")
    data = path.read_bytes()
    assert data.startswith(existing_bytes)
    assert b"command_root=hostname" in data


def test_externally_rotated_audit_log_reopens_to_new_0600_inode_without_truncating_archive(
    tmp_path, isolated_audit,
):
    path = tmp_path / "audit.log"
    archive = tmp_path / "audit.log.1"
    audit = AuditLogger(str(path))
    audit.log_auth("first", True, "127.0.0.1")
    old_inode = path.stat().st_ino
    old_bytes = path.read_bytes()
    path.rename(archive)
    assert archive.stat().st_ino == old_inode
    path.write_text("new external header\\n", encoding="utf-8")
    path.chmod(0o666)
    audit.log_auth("second", True, "127.0.0.1")
    assert _mode(path) == 0o600
    assert path.stat().st_ino != old_inode
    assert archive.read_bytes() == old_bytes
    assert path.read_text(encoding="utf-8").startswith("new external header\\n")
    assert "AUTH | user=second" in path.read_text(encoding="utf-8")


def test_symlinked_audit_destination_is_denied_without_touching_target(tmp_path, isolated_audit):
    target = tmp_path / "protected"
    target.write_text("DO NOT MODIFY", encoding="utf-8")
    link = tmp_path / "audit.log"
    link.symlink_to(target)
    with pytest.raises(OSError):
        AuditLogger(str(link))
    assert target.read_text(encoding="utf-8") == "DO NOT MODIFY"


def test_hardlinked_audit_file_is_denied_without_repairing_other_inode(
    tmp_path, isolated_audit,
):
    target = tmp_path / "protected"
    target.write_text("DO NOT MODIFY", encoding="utf-8")
    target.chmod(0o666)
    log = tmp_path / "audit.log"
    os.link(target, log)
    with pytest.raises(PermissionError, match="regular inode"):
        AuditLogger(str(log))
    assert target.read_text(encoding="utf-8") == "DO NOT MODIFY"
    assert _mode(target) == 0o666


def test_audit_directory_symlink_is_denied_without_writing_through_it(
    tmp_path, isolated_audit,
):
    target_dir = tmp_path / "other"
    target_dir.mkdir()
    link = tmp_path / "logs"
    link.symlink_to(target_dir, target_is_directory=True)
    with pytest.raises(OSError):
        AuditLogger(str(link / "audit.log"))
    assert not (target_dir / "audit.log").exists()


def test_insecure_log_replacement_with_symlink_refuses_rotation(
    tmp_path, isolated_audit,
):
    path = tmp_path / "audit.log"
    rotated = tmp_path / "audit.log.old"
    victim = tmp_path / "victim"
    victim.write_text("protected", encoding="utf-8")
    audit = AuditLogger(str(path))
    audit.log_auth("first", True, "127.0.0.1")
    path.rename(rotated)
    path.symlink_to(victim)
    with pytest.raises(OSError):
        audit.log_auth("second", True, "127.0.0.1")
    assert victim.read_text(encoding="utf-8") == "protected"
    assert b"first" in rotated.read_bytes()


def test_preupgrade_insecure_file_handler_cannot_bypass_secure_reopen(
    tmp_path, isolated_audit,
):
    path = tmp_path / "audit.log"
    old_handler = logging.FileHandler(path)
    isolated_audit.addHandler(old_handler)
    with pytest.raises(PermissionError, match="legacy audit"):
        AuditLogger(str(path))
    # pytest itself installs LogCaptureHandler instances on this logger.
    assert sum(isinstance(h, logging.FileHandler) for h in isolated_audit.handlers) == 1
    assert path.exists()


def test_regular_file_owned_by_other_uid_is_not_chmodded_or_written(
    tmp_path, isolated_audit, monkeypatch,
):
    path = tmp_path / "audit.log"
    path.write_text("prior event", encoding="utf-8")
    path.chmod(0o644)
    true_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: true_uid + 1)
    with pytest.raises(PermissionError, match="owner"):
        AuditLogger(str(path))
    assert _mode(path) == 0o644
    assert path.read_text(encoding="utf-8") == "prior event"


def test_duplicate_audit_logger_reuses_secure_handler_and_no_duplicate_line(
    tmp_path, isolated_audit,
):
    path = tmp_path / "audit.log"
    first = AuditLogger(str(path))
    second = AuditLogger(str(path))
    handlers = [
        h for h in isolated_audit.handlers
        if isinstance(h, _SecureAuditFileHandler)
    ]
    assert len(handlers) == 1
    first.log_auth("single", True, "127.0.0.1")
    assert path.read_text(encoding="utf-8").count("AUTH | user=single") == 1
    assert second.logger is first.logger
