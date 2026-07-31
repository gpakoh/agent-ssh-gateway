from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

BACKUP_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="borg-delete",
        regex=r"borg(?:\s+--?\S+(?:\s+\S+)?)*\s+delete\b",
        reason="borg delete removes archives from the repository",
        severity=Severity.CRITICAL,
        description="borg delete removes archive(s) from the backup repository. Deleted "
        "archives are permanently gone — the only copy of the data may be destroyed.",
        suggestions=(
            PatternSuggestion(command="borg list {repo}", description="List archives before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="borg export-tar {repo}::{archive} -C zstd > {file}", description="Export the archive before deleting it", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="borg-prune",
        regex=r"borg(?:\s+--?\S+(?:\s+\S+)?)*\s+prune\b",
        reason="borg prune removes archives per retention policy",
        severity=Severity.HIGH,
        description="borg prune permanently deletes archives matching the retention "
        "policy (e.g. --keep-daily 7). Wrong policy values delete more history than "
        "intended, irreversibly.",
        suggestions=(
            PatternSuggestion(command="borg list {repo}", description="Review archives before pruning", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="borg prune --dry-run --list {repo}", description="Preview which archives would be removed", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="borg-compact",
        regex=r"borg(?:\s+--?\S+(?:\s+\S+)?)*\s+compact\b",
        reason="borg compact reclaims space from deleted data",
        severity=Severity.MEDIUM,
        description="borg compact permanently frees space used by deleted or updated "
        "chunks. Safe after verification, but irreversible.",
        suggestions=(
            PatternSuggestion(command="borg info {repo}", description="Check repository usage first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="borg check {repo}", description="Verify repository health before compacting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="borg-recreate",
        regex=r"borg(?:\s+--?\S+(?:\s+\S+)?)*\s+recreate\b",
        reason="borg recreate rewrites archives in place",
        severity=Severity.HIGH,
        description="borg recreate rewrites archives applying filters (exclude, "
        "compression). If interrupted, archives can be corrupted.",
        suggestions=(
            PatternSuggestion(command="borg list {repo}", description="Review archives before recreating", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="borg create {repo}::{archive} {src}", description="Create a new archive with filters instead", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="borg-break-lock",
        regex=r"borg(?:\s+--?\S+(?:\s+\S+)?)*\s+break-lock\b",
        reason="borg break-lock forcibly removes repository locks",
        severity=Severity.MEDIUM,
        description="borg break-lock forcibly removes stale locks. If another process is "
        "actively writing, this can corrupt the repository.",
        suggestions=(
            PatternSuggestion(command="ps aux | grep borg", description="Verify no borg process is running first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="borg lock kill {repo} --force", description="Use the explicit lock subcommand", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="restic-forget",
        regex=r"restic\b.*\sforget\b",
        reason="restic forget removes snapshots per retention policy",
        severity=Severity.CRITICAL,
        description="restic forget permanently removes snapshots matching the retention "
        "policy. Bad policy values can delete ALL backups irreversibly.",
        suggestions=(
            PatternSuggestion(command="restic snapshots", description="List snapshots before forgetting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="restic forget --dry-run {policy_args}", description="Preview which snapshots would be removed", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="restic-prune",
        regex=r"restic\b.*\sprune\b",
        reason="restic prune removes unreferenced data from the repository",
        severity=Severity.CRITICAL,
        description="restic prune permanently deletes data no longer referenced by "
        "snapshots. After prune, forgotten snapshots are unrecoverable.",
        suggestions=(
            PatternSuggestion(command="restic snapshots --latest 3", description="Verify recent snapshots first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="restic check", description="Verify repository health before pruning", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="restic-key-remove",
        regex=r"restic\b.*\skey\b.*\sremove\b",
        reason="restic key remove deletes an encryption key",
        severity=Severity.CRITICAL,
        description="restic key remove deletes a repository key. If the deleted key is "
        "the only one, all backup data becomes permanently undecryptable.",
        suggestions=(
            PatternSuggestion(command="restic key list", description="List all keys first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="restic key add", description="Add a replacement key before removing", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="restic-unlock-remove-all",
        regex=r"restic\b.*\sunlock\b.*\s--remove-all\b",
        reason="restic unlock --remove-all force-removes all locks",
        severity=Severity.HIGH,
        description="restic unlock --remove-all forcibly removes all locks without "
        "verifying staleness. Active processes may be corrupted.",
        suggestions=(
            PatternSuggestion(command="restic list locks", description="Review locks before removing", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="restic unlock", description="Remove only stale locks (default behavior)", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="restic-cache-cleanup",
        regex=r"restic\b.*\scache\b.*\s--cleanup\b",
        reason="restic cache --cleanup removes cached data",
        severity=Severity.LOW,
        description="restic cache --cleanup removes cached repository data. Only affects "
        "performance, but removes the local copy of pack files.",
        suggestions=(
            PatternSuggestion(command="restic cache --info", description="Check cache status first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="rm -rf ~/.cache/restic", description="Manually remove the cache after verifying", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="rclone-sync",
        regex=r"rclone(?:\s+--?\S+(?:\s+\S+)?)*\s+sync\b",
        reason="rclone sync makes dest identical to src, deleting extras",
        severity=Severity.CRITICAL,
        description="rclone sync deletes files in the destination that do not exist in "
        "the source. Misconfigured paths can wipe the destination entirely.",
        suggestions=(
            PatternSuggestion(command="rclone ls {dest}", description="Review destination contents first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="rclone copy {src} {dest} --dry-run", description="Copy instead of sync — destination is preserved", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="rclone-delete",
        regex=r"rclone(?:\s+--?\S+(?:\s+\S+)?)*\s+delete\b",
        reason="rclone delete removes files from the destination",
        severity=Severity.HIGH,
        description="rclone delete permanently removes files matching the filter from "
        "the destination. No trash on most remotes.",
        suggestions=(
            PatternSuggestion(command="rclone lsl {dest}", description="List files before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="rclone move {src} {dest} --dry-run", description="Use --dry-run to preview deletions", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="rclone-deletefile",
        regex=r"rclone(?:\s+--?\S+(?:\s+\S+)?)*\s+deletefile\b",
        reason="rclone deletefile removes a single file",
        severity=Severity.HIGH,
        description="rclone deletefile permanently removes a single file from the "
        "destination. Cannot be recovered on most remotes.",
        suggestions=(
            PatternSuggestion(command="rclone lsl {file}", description="Verify the file before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="rclone copy {src} {dest}", description="Re-upload the file after verification", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="rclone-purge",
        regex=r"rclone(?:\s+--?\S+(?:\s+\S+)?)*\s+purge\b",
        reason="rclone purge removes an entire directory tree",
        severity=Severity.CRITICAL,
        description="rclone purge permanently deletes a directory and all contents. "
        "Equivalent to rm -rf on the remote.",
        suggestions=(
            PatternSuggestion(command="rclone ls {dir}", description="List directory contents first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="rclone delete {dir} --filter '!*.keep'", description="Delete selectively with filters", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="rclone-cleanup",
        regex=r"rclone(?:\s+--?\S+(?:\s+\S+)?)*\s+cleanup\b",
        reason="rclone cleanup removes old files from a bucket",
        severity=Severity.MEDIUM,
        description="rclone cleanup removes versioned objects older than their retention "
        "in bucket remotes. Permanent after completion.",
        suggestions=(
            PatternSuggestion(command="rclone versioning {remote}:", description="Check versioning state first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="rclone ls --max-age 90d {remote}:", description="Review objects that would be cleaned", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="rclone-dedupe",
        regex=r"rclone(?:\s+--?\S+(?:\s+\S+)?)*\s+dedupe\b",
        reason="rclone dedupe removes duplicate files",
        severity=Severity.HIGH,
        description="rclone dedupe removes duplicate files in a directory, keeping one "
        "per hash. Wrong mode can delete the wrong copy.",
        suggestions=(
            PatternSuggestion(command="rclone check {dir} {dir}", description="Review duplicates first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="rclone dedupe --dedupe-mode=rename {dir}", description="Rename duplicates instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="rclone-move",
        regex=r"rclone(?:\s+--?\S+(?:\s+\S+)?)*\s+move\b",
        reason="rclone move moves files, deleting them from the source",
        severity=Severity.MEDIUM,
        description="rclone move transfers files to the destination AND deletes them "
        "from the source. Interrupted transfers can lose files.",
        suggestions=(
            PatternSuggestion(command="rclone copy {src} {dest} --dry-run", description="Copy instead of move — source preserved", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="rclone lsl {src}", description="Verify source contents first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="velero-backup-delete",
        regex=r"velero(?:\s+--?\S+(?:\s+\S+)?)*\s+backup\s+delete\b",
        reason="velero backup delete removes backups from the cluster",
        severity=Severity.HIGH,
        description="velero backup delete permanently removes backups and their cloud "
        "objects. Restore becomes impossible after deletion.",
        suggestions=(
            PatternSuggestion(command="velero backup describe {name} --details", description="Review backup details first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="velero backup get", description="List all backups before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="velero-schedule-delete",
        regex=r"velero(?:\s+--?\S+(?:\s+\S+)?)*\s+schedule\s+delete\b",
        reason="velero schedule delete removes a backup schedule",
        severity=Severity.HIGH,
        description="velero schedule delete removes a scheduled backup. Future backups "
        "stop being created — data protection window is lost.",
        suggestions=(
            PatternSuggestion(command="velero schedule describe {name}", description="Review the schedule first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="velero schedule pause {name}", description="Pause instead of delete — reversible", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="velero-restore-delete",
        regex=r"velero(?:\s+--?\S+(?:\s+\S+)?)*\s+restore\s+delete\b",
        reason="velero restore delete removes restore records",
        severity=Severity.MEDIUM,
        description="velero restore delete removes restore objects from the cluster. "
        "Restores already performed are unaffected, but the record is gone.",
        suggestions=(
            PatternSuggestion(command="velero restore get", description="List restores first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="velero restore create --from-backup {backup}", description="Create a fresh restore instead", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="duplicity-remove-older-than",
        regex=r"duplicity\b.*\bremove-older-than\b",
        reason="duplicity remove-older-than deletes old backup chains",
        severity=Severity.CRITICAL,
        description="duplicity remove-older-than permanently deletes backup chains older "
        "than the given time. Full chains are deleted including their bases.",
        suggestions=(
            PatternSuggestion(command="duplicity collection-status {url}", description="Review the backup chain first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="duplicity list-current-files {url}", description="List recoverable files before cleanup", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="duplicity-delete",
        regex=r"duplicity\b.*\bdelete\b",
        reason="duplicity delete removes backup chains for a file set",
        severity=Severity.HIGH,
        description="duplicity delete removes backup chains matching the given file "
        "set. The affected data becomes unrecoverable.",
        suggestions=(
            PatternSuggestion(command="duplicity collection-status {url}", description="Review the backup chain first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="duplicity restore --file-to-restore {path} {url} {target}", description="Restore the data before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
)


def build_backup_pack() -> Pack:
    return Pack(
        id="backup",
        name="Backup",
        destructive_patterns=BACKUP_PATTERNS,
        keywords=("borg", "restic", "rclone", "velero", "duplicity"),
    )
