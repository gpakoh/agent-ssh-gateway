"""Storage pack destructive pattern tests (P18)."""

from __future__ import annotations

from app.packs.registry import build_registry


class TestStoragePack:
    def test_storage_pack_patterns(self):
        """Storage pack (P18) covers zfs/zpool, s3 sync --delete, gcs, minio, azure."""
        r = build_registry()
        cases = {
            "zfs destroy tank/data": "zfs-destroy",
            "zpool destroy tank": "zpool-destroy",
            "zpool remove tank /dev/sdb": "zpool-remove",
            "aws s3 sync s3://src s3://dst --delete": "s3-sync-delete",
            "aws s3api delete-objects --bucket b --delete '{\"Objects\":[{\"Key\":\"k\"}]}'": "s3api-delete-objects",
            "gcloud storage buckets delete gs://mybucket --recursive": "gcloud-storage-buckets-delete",
            "gcloud storage rm -r gs://mybucket/path": "gcloud-storage-rm",
            "gsutil rsync -d gs://src gs://dst": "gsutil-rsync-delete",
            "mc rb --force --recursive myminio/backups": "mc-rb",
            "mc admin bucket delete myminio/old": "mc-admin-bucket-delete",
            "mc mirror --remove --overwrite src dst": "mc-mirror-remove",
            "mc admin user remove myminio olduser": "mc-admin-user-remove",
            "az storage account delete -n myacct -g myrg --yes": "az-storage-account-delete",
            "az storage container delete -n mycont --account-name myacct": "az-storage-container-delete",
            "azcopy remove https://acct.blob.core.windows.net/cont?SAS --recursive": "azcopy-remove",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_storage_pack_reads_not_blocked(self):
        """Read/list operations on storage tools must NOT be blocked."""
        r = build_registry()
        for cmd in (
            "zfs list",
            "zfs list -t snapshot -r tank",
            "zpool status tank",
            "zpool list",
            "aws s3 ls",
            "aws s3 sync s3://src s3://dst --dryrun",
            "gcloud storage buckets describe gs://mybucket",
            "gcloud storage ls -r gs://mybucket",
            "gsutil ls gs://bucket",
            "gsutil rsync -n -d gs://src gs://dst",
            "mc ls myminio/bucket",
            "mc mirror --dry-run --remove src dst",
            "az storage account list",
            "az storage container list --account-name myacct",
            "azcopy list 'https://acct.blob.core.windows.net/cont?SAS'",
        ):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"
