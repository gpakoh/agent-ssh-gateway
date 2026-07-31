"""Backup pack destructive pattern tests (P18)."""

from __future__ import annotations

from app.packs.registry import build_registry


class TestBackupPack:
    def test_backup_pack_patterns(self):
        """Backup pack (P18) covers borg, restic, rclone, velero, duplicity."""
        r = build_registry()
        cases = {
            "borg delete repo::old": "borg-delete",
            "borg prune --keep-daily 7 repo": "borg-prune",
            "restic forget --keep-daily 7": "restic-forget",
            "restic prune": "restic-prune",
            "restic key remove 1": "restic-key-remove",
            "rclone sync src: dest:": "rclone-sync",
            "rclone purge remote:dir": "rclone-purge",
            "rclone dedupe remote:dir": "rclone-dedupe",
            "velero backup delete --yes": "velero-backup-delete",
            "velero schedule delete daily": "velero-schedule-delete",
            "duplicity remove-older-than 30D file:///backup": "duplicity-remove-older-than",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_backup_pack_reads_not_blocked(self):
        """Read/list/dry-run operations on backup tools must NOT be blocked."""
        r = build_registry()
        for cmd in (
            "borg list repo",
            "borg info repo",
            "borg check repo",
            "restic snapshots",
            "restic check",
            "restic list snapshots",
            "rclone copy src dest --dry-run",
            "rclone ls remote:dir",
            "velero backup get",
            "velero restore get",
            "duplicity collection-status file:///backup",
        ):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"
