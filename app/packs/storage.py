from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

STORAGE_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="zfs-destroy",
        regex=r"\bzfs\b(?:\s+--?\S+(?:\s+\S+)?)*\s+destroy\b",
        reason="zfs destroy permanently removes a dataset or snapshot",
        severity=Severity.CRITICAL,
        description="zfs destroy deletes a dataset, volume or snapshot and all its "
        "data. With -r it removes all children, with -R also dependent snapshots. "
        "Irreversible without a replication target.",
        suggestions=(
            PatternSuggestion(command="zfs list -t filesystem,volume,snapshot -r {pool}", description="List datasets and snapshots first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="zfs snapshot {pool}@{ts} && zfs send {pool}@{ts} | gzip > backup.zfs.gz", description="Snapshot and send to a file before destroying", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="zfs-rollback",
        regex=r"\bzfs\b(?:\s+--?\S+(?:\s+\S+)?)*\s+rollback\b(?!.*--dryrun)",
        reason="zfs rollback discards all changes made since the snapshot",
        severity=Severity.HIGH,
        description="zfs rollback reverts a dataset to a snapshot, discarding all "
        "changes made after it. With -r it also destroys intermediate snapshots. "
        "Current state is lost.",
        suggestions=(
            PatternSuggestion(command="zfs list -t snapshot -r {pool}", description="List snapshots and confirm the target", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="zfs snapshot {pool}@pre-rollback", description="Take a safety snapshot before rollback", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="zpool-destroy",
        regex=r"\bzpool\b(?:\s+--?\S+(?:\s+\S+)?)*\s+destroy\b",
        reason="zpool destroy permanently removes a storage pool",
        severity=Severity.CRITICAL,
        description="zpool destroy deletes an entire pool and ALL datasets, snapshots "
        "and data on it. The devices become free. Irreversible.",
        suggestions=(
            PatternSuggestion(command="zpool status {pool} && zpool list", description="Review pool health and usage first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="zfs snapshot -r {pool}@backup && zfs send -R {pool}@backup | gzip > pool-backup.zfs.gz", description="Send the full recursive stream before destroying", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="zpool-remove",
        regex=r"\bzpool\b(?:\s+--?\S+(?:\s+\S+)?)*\s+remove\b",
        reason="zpool remove detaches a vdev from the pool",
        severity=Severity.HIGH,
        description="zpool remove permanently detaches a device from a pool. Data is "
        "rewritten to remaining vdevs; removing a vdev can reduce redundancy and "
        "can fail or take a long time.",
        suggestions=(
            PatternSuggestion(command="zpool status {pool}", description="Check pool status and vdev layout first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="zpool offline {pool} {device} && zpool status {pool}", description="Offline the device and verify before removal", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="s3-sync-delete",
        regex=r"aws(?:\s+--?\S+(?:\s+\S+)?)*\s+s3\s+sync\b(?:\s+[^\n]*)?\s+--delete\b(?![\s\S]*(?:--dryrun|--dry-run)\b)",
        reason="aws s3 sync --delete removes destination objects not in source",
        severity=Severity.HIGH,
        description="The --delete flag removes files from the destination that don't "
        "exist in the source. If source and destination are swapped, or the source "
        "is empty, this deletes everything at the destination.",
        suggestions=(
            PatternSuggestion(command="aws s3 sync {src} {dst} --dryrun", description="Preview changes before syncing", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws s3 sync {src} {dst}", description="Sync without --delete for additive-only updates", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="s3api-delete-objects",
        regex=r"aws(?:\s+--?\S+(?:\s+\S+)?)*\s+s3api\s+delete-objects\b",
        reason="aws s3api delete-objects permanently deletes multiple objects",
        severity=Severity.HIGH,
        description="delete-objects batch-deletes objects from an S3 bucket. For "
        "unversioned buckets the data is permanently lost. A typo in the key list "
        "can wipe the wrong objects.",
        suggestions=(
            PatternSuggestion(command="aws s3api list-objects-v2 --bucket {bucket} --query 'Contents[].Key'", description="Verify object keys before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws s3api get-object-attributes --bucket {bucket} --key {key}", description="Confirm the exact key before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="gcloud-storage-buckets-delete",
        regex=r"gcloud\b(?:\s+--?\S+(?:\s+\S+)?)*(?:\s+(?:alpha|beta)(?:\s+--?\S+(?:\s+\S+)?)*)?\s+storage\s+buckets\s+delete\b",
        reason="gcloud storage buckets delete removes a GCS bucket",
        severity=Severity.CRITICAL,
        description="Deleting a GCS bucket removes the bucket configuration and all "
        "objects within it (with --recursive). The bucket name may not be "
        "reclaimable; all dependent applications fail.",
        suggestions=(
            PatternSuggestion(command="gcloud storage buckets describe {bucket}", description="Review bucket configuration first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gcloud storage ls -r gs://{bucket} > objects.txt", description="Back up the object list before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="gcloud-storage-objects-delete",
        regex=r"gcloud\b(?:\s+--?\S+(?:\s+\S+)?)*(?:\s+(?:alpha|beta)(?:\s+--?\S+(?:\s+\S+)?)*)?\s+storage\s+objects\s+delete\b",
        reason="gcloud storage objects delete removes objects from GCS",
        severity=Severity.HIGH,
        description="Deleting GCS objects permanently removes data. Without object "
        "versioning, deleted files cannot be recovered.",
        suggestions=(
            PatternSuggestion(command="gcloud storage objects describe {uri}", description="Verify the object before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gcloud storage cp {uri} {backup}", description="Copy the object to a backup location first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="gcloud-storage-rm",
        regex=r"gcloud\b(?:\s+--?\S+(?:\s+\S+)?)*(?:\s+(?:alpha|beta)(?:\s+--?\S+(?:\s+\S+)?)*)?\s+storage\s+rm\b(?![\s\S]*--dryrun\b)",
        reason="gcloud storage rm removes objects from GCS",
        severity=Severity.HIGH,
        description="The rm command deletes objects and can recursively remove entire "
        "bucket trees with -r. Without versioning, data is permanently lost.",
        suggestions=(
            PatternSuggestion(command="gcloud storage ls -r {uri}", description="Preview what will be removed", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gcloud storage cp -r {uri} {backup}", description="Copy objects to a backup location first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="gsutil-rsync-delete",
        regex=r"gsutil\b.*?\brsync\b(?![\s\S]*\s+-n\b)[\s\S]*\s+-d\b",
        reason="gsutil rsync -d deletes destination objects not in source",
        severity=Severity.HIGH,
        description="The -d flag deletes destination objects that don't exist in the "
        "source. If source and destination are swapped, or source is empty, this "
        "results in total data loss at the destination.",
        suggestions=(
            PatternSuggestion(command="gsutil rsync -n -d {src} {dst}", description="Dry run to preview deletions", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gsutil rsync {src} {dst}", description="Sync without -d for additive-only updates", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="mc-rb",
        regex=r"\bmc\s+(?:--?\S+\s+)*rb\b",
        reason="mc rb removes a MinIO/S3 bucket",
        severity=Severity.CRITICAL,
        description="mc rb deletes an entire bucket and, with --force --recursive, "
        "all objects in it. Data is permanently lost.",
        suggestions=(
            PatternSuggestion(command="mc ls {alias}/{bucket}", description="List bucket contents first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="mc mirror {alias}/{bucket} {backup-alias}/{bucket}", description="Mirror the bucket to a backup location before removal", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="mc-rm",
        regex=r"\bmc\s+(?:--?\S+\s+)*rm\b",
        reason="mc rm deletes objects from MinIO/S3",
        severity=Severity.HIGH,
        description="mc rm permanently deletes objects. With --recursive it removes "
        "entire prefixes. Without versioning, data cannot be recovered.",
        suggestions=(
            PatternSuggestion(command="mc ls --recursive {alias}/{bucket}", description="Preview objects before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="mc mirror {alias}/{bucket}/obj {backup-alias}/{backup}", description="Copy objects to a backup location first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="mc-admin-bucket-delete",
        regex=r"\bmc\s+(?:--?\S+\s+)*admin\s+bucket\s+(?:delete|remove)\b",
        reason="mc admin bucket delete removes a MinIO bucket",
        severity=Severity.CRITICAL,
        description="mc admin bucket delete removes a bucket on the MinIO server. "
        "All objects within it are deleted; the operation is irreversible.",
        suggestions=(
            PatternSuggestion(command="mc ls {alias}/{bucket}", description="List bucket contents first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="mc mirror {alias}/{bucket} {backup-alias}/{backup}", description="Mirror the bucket to a backup location before removal", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="mc-mirror-remove",
        regex=r"\bmc\s+(?:--?\S+\s+)*mirror\b(?![\s\S]*--dry-run\b)[\s\S]*--remove\b",
        reason="mc mirror --remove deletes destination objects not in source",
        severity=Severity.HIGH,
        description="mc mirror --remove deletes destination objects that don't exist "
        "in the source, making destinations identical to sources. A swap or empty "
        "source destroys destination data.",
        suggestions=(
            PatternSuggestion(command="mc mirror --dry-run --remove {src} {dst}", description="Preview changes before mirroring", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="mc mirror {src} {dst}", description="Mirror without --remove for additive-only copies", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="mc-admin-user-remove",
        regex=r"\bmc\s+(?:--?\S+\s+)*admin\s+user\s+(?:remove|disable)\b",
        reason="mc admin user remove/disable deletes a MinIO user",
        severity=Severity.HIGH,
        description="Removes or disables a MinIO user. Applications using its "
        "credentials stop working immediately; audit trails lose the identity.",
        suggestions=(
            PatternSuggestion(command="mc admin user info {alias} {user}", description="Verify the user and their access first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="mc admin user disable {alias} {user}", description="Disable instead of remove to keep audit history", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="mc-admin-policy-remove",
        regex=r"\bmc\s+(?:--?\S+\s+)*admin\s+policy\s+(?:remove|unset)\b",
        reason="mc admin policy remove/unset deletes a MinIO policy",
        severity=Severity.MEDIUM,
        description="Removes or unsets a MinIO policy. Users depending on it lose "
        "their access rights, which can break running applications.",
        suggestions=(
            PatternSuggestion(command="mc admin policy info {alias} {policy}", description="Review the policy before removal", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="mc admin policy export {alias} {policy} > policy.json", description="Export the policy before removal", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="az-storage-account-delete",
        regex=r"\baz\b(?:\s+--?\S+(?:\s+\S+)?)*\s+storage\s+account\s+delete\b",
        reason="az storage account delete removes an Azure Storage account",
        severity=Severity.CRITICAL,
        description="Deletes an Azure Storage account with ALL its containers, blobs, "
        "tables and queues. Soft-delete may allow recovery within retention, but "
        "otherwise the data is gone.",
        suggestions=(
            PatternSuggestion(command="az storage account show --name {acct} --resource-group {rg}", description="Review account configuration first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="azcopy sync '{src}' 'https://{backup}.blob.core.windows.net/{container}?{sas}' --recursive", description="Back up account data before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="az-storage-container-delete",
        regex=r"\baz\b(?:\s+--?\S+(?:\s+\S+)?)*\s+storage\s+container\s+delete\b",
        reason="az storage container delete removes an Azure blob container",
        severity=Severity.CRITICAL,
        description="Deletes a blob container and all blobs within it. Only recoverable "
        "if container soft-delete is enabled for the account.",
        suggestions=(
            PatternSuggestion(command="az storage blob list --container-name {container} --account-name {acct}", description="List blobs before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="azcopy sync '{src}' 'https://{acct}.blob.core.windows.net/{container}?{sas}' --recursive", description="Back up container data before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="az-storage-blob-delete-batch",
        regex=r"\baz\b(?:\s+--?\S+(?:\s+\S+)?)*\s+storage\s+blob\s+delete-batch\b",
        reason="az storage blob delete-batch deletes multiple blobs",
        severity=Severity.HIGH,
        description="delete-batch removes multiple blobs at once, optionally "
        "recursively over a prefix. A wrong prefix deletes far more than intended.",
        suggestions=(
            PatternSuggestion(command="az storage blob list --container-name {container} --prefix {prefix}", description="List blobs under the prefix first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="azcopy sync '{src}' 'https://{acct}.blob.core.windows.net/{container}?{sas}' --recursive", description="Back up blobs before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="azcopy-remove",
        regex=r"\bazcopy\s+(?:--\S+\s+)*remove\b",
        reason="azcopy remove deletes blobs or files",
        severity=Severity.HIGH,
        description="azcopy remove deletes blobs/files at the target path, recursively "
        "with --recursive, and permanently when --delete-snapshots include is used.",
        suggestions=(
            PatternSuggestion(command="azcopy list '{src}?{sas}'", description="List objects before removal", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="azcopy copy '{src}?{sas}' '{backup}?{sas}' --recursive", description="Copy to a backup location before removal", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="azcopy-sync-delete",
        regex=r"\bazcopy\s+(?:--\S+\s+)*sync\b.*--delete-destination\b",
        reason="azcopy sync --delete-destination deletes destination objects not in source",
        severity=Severity.HIGH,
        description="azcopy sync with --delete-destination removes destination blobs "
        "that don't exist in the source. A wrong source or a full/empty source "
        "destroys the destination.",
        suggestions=(
            PatternSuggestion(command="azcopy sync '{src}?{sas}' '{dst}?{sas}' --delete-destination=false", description="Sync additively without deleting first", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="azcopy sync '{src}?{sas}' '{dst}?{sas}' --delete-destination=prompt", description="Prompt before each deletion", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
)


def build_storage_pack() -> Pack:
    return Pack(
        id="storage",
        name="Storage",
        destructive_patterns=STORAGE_PATTERNS,
        keywords=("zfs", "zpool", "aws s3", "s3api", "gcloud storage", "gsutil", "mc ", "az storage", "azcopy"),
    )
