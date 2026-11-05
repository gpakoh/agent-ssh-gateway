"""Real unnamed-file spool and exact-lease negative scenarios for private ZIPs."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import stat
import zipfile

import pytest

from examples.mcp_client_remote.fleet.gitea_artifacts_spool import (
    ArtifactLease,
    ArtifactSpool,
    ArtifactTransportError,
)

HEAD = "a" * 40
DATA = io.BytesIO()
with zipfile.ZipFile(DATA, "w") as z:
    z.writestr("preview/index.html", "<html>Private</html>")
ZIP = DATA.getvalue()
DIGEST = hashlib.sha256(ZIP).hexdigest()


class Fake:
    def __init__(self):
        self.pr = {
            "number": 54, "state": "open", "merged": False,
            "head": {"sha": HEAD, "repo": {"full_name": "gpakoh/xloud"}},
        }
        self.run = {
            "id": 200, "head_sha": HEAD, "run_attempt": 2,
            "event": "pull_request", "status": "completed", "conclusion": "success",
            "repository": {"full_name": "gpakoh/xloud"},
        }
        self.artifact = {
            "id": 456, "name": "preview-bundle", "size_in_bytes": len(ZIP),
            "expired": False, "expires_at": "2099-11-07T00:00:00Z",
            "workflow_run": {"id": 200, "head_sha": HEAD, "run_attempt": 2},
            "archive_download_url": "https://signed.example/private?token=SECRET",
        }
        self.calls: list[str] = []
        self.on_download = None

    async def get_pull_request(self, *_):
        self.calls.append("pr")
        return self.pr

    async def get_action_run(self, *_):
        self.calls.append("run")
        return self.run

    async def list_action_run_artifacts(self, *_):
        self.calls.append("list")
        return {"total_count": 1, "artifacts": [self.artifact]}

    async def download_action_artifact_zip(self, owner, repo, artifact_id, output, **kwargs):
        self.calls.append("download")
        assert artifact_id == 456
        if self.on_download is not None:
            await self.on_download()
        output.write(ZIP)
        return {"sha256": DIGEST, "size_bytes": len(ZIP), "entries": 1}


@pytest.fixture
def lease():
    return ArtifactLease(
        "gpakoh", "xloud", 54, 200, HEAD, 2, 456, expected_sha256=DIGEST
    )


@pytest.fixture
def spool(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    os.chmod(tmp_path, 0o700)
    service = ArtifactSpool(tmp_path, root, ttl_s=1, max_handles=1)
    try:
        yield service
    finally:
        service.close()


async def stage(spool, fake, lease):
    return await spool.stage(
        fake, lease, redirect_origins=["https://artifacts.example.test"]
    )


@pytest.mark.asyncio
async def test_real_private_unnamed_inode_one_time_inspection_and_provenance(spool, lease):
    fake = Fake()
    staged = await stage(spool, fake, lease)
    assert fake.calls == ["pr", "run", "list", "download", "pr", "run", "list"]
    assert staged["size_bytes"] == len(ZIP)
    assert staged["sha256"] == DIGEST
    assert "SECRET" not in str(staged)
    assert "private" not in str(staged)
    assert staged["handle"] not in fake.calls

    seen = []

    async def inspect(file):
        info = os.fstat(file.fileno())
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_nlink == 0
        seen.append(hashlib.sha256(file.read()).hexdigest())

    proof = await spool.inspect_once(fake, staged["handle"], inspect)
    assert seen == [DIGEST]
    assert proof["sha256"] == DIGEST
    assert fake.calls == [
        "pr", "run", "list", "download", "pr", "run", "list",
        "pr", "run", "list", "pr", "run", "list",
    ]
    with pytest.raises(ArtifactTransportError, match="unavailable"):
        await spool.inspect_once(fake, staged["handle"], inspect)
    assert not spool.revoke(staged["handle"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "stage_error"),
    [
        (lambda f: f.pr.update(state="closed"), "open"),
        (lambda f: f.pr.update(head={"sha": "b" * 40, "repo": {"full_name": "gpakoh/xloud"}}), "HEAD"),
        (lambda f: f.pr["head"].update(repo={"full_name": "attacker/xloud"}), "HEAD"),
        (lambda f: f.run.update(run_attempt=3), "run"),
        (lambda f: f.run.update(conclusion="failure"), "run"),
        (lambda f: f.run.update(event="push"), "run"),
        (lambda f: f.artifact["workflow_run"].pop("run_attempt"), "attempt"),
        (lambda f: f.artifact.update(expired=True), "expired"),
        (lambda f: f.artifact.update(expires_at="2020-01-01T00:00:00Z"), "expired"),
        (lambda f: f.artifact.update(size_in_bytes=21 * 1024 * 1024), "bounded"),
        (lambda f: f.artifact.update(id=999), "no longer exists"),
    ],
)
async def test_bad_provenance_or_expiry_refuses_before_download(
    spool, lease, mutation, stage_error
):
    fake = Fake()
    mutation(fake)
    with pytest.raises(ArtifactTransportError, match=stage_error):
        await stage(spool, fake, lease)
    assert "download" not in fake.calls
    assert not spool._entries
    assert spool._pending == 0


@pytest.mark.asyncio
async def test_post_download_drift_revokes_before_handle_issued(spool, lease):
    fake = Fake()

    async def alter():
        fake.run["run_attempt"] = 3

    fake.on_download = alter
    with pytest.raises(ArtifactTransportError, match="run evidence"):
        await stage(spool, fake, lease)
    assert fake.calls.count("download") == 1
    assert not spool._entries


@pytest.mark.asyncio
async def test_post_download_artifact_size_drift_revokes(spool, lease):
    fake = Fake()

    async def alter():
        fake.artifact["size_in_bytes"] += 1

    fake.on_download = alter
    with pytest.raises(ArtifactTransportError, match="metadata changed"):
        await stage(spool, fake, lease)
    assert not spool._entries


@pytest.mark.asyncio
async def test_consume_refuses_if_run_changes_after_stage(spool, lease):
    fake = Fake()
    staged = await stage(spool, fake, lease)
    fake.run["run_attempt"] = 3
    called = []

    async def inspect(_):
        called.append(1)

    with pytest.raises(ArtifactTransportError, match="run evidence"):
        await spool.inspect_once(fake, staged["handle"], inspect)
    assert called == []
    assert not spool._entries


@pytest.mark.asyncio
async def test_consume_refuses_post_inspection_drift(spool, lease):
    fake = Fake()
    staged = await stage(spool, fake, lease)

    async def inspect(file):
        assert file.read() == ZIP
        fake.pr["state"] = "closed"

    with pytest.raises(ArtifactTransportError, match="open"):
        await spool.inspect_once(fake, staged["handle"], inspect)
    assert not spool._entries


@pytest.mark.asyncio
async def test_ttl_revokes_fd_even_without_any_followup_request(spool, lease):
    fake = Fake()
    staged = await stage(spool, fake, lease)
    file = spool._entries[staged["handle"]].file
    assert not file.closed
    await asyncio.sleep(1.08)
    assert file.closed
    assert not spool._entries
    with pytest.raises(ArtifactTransportError, match="unavailable"):
        await spool.inspect_once(fake, staged["handle"], lambda _: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_capacity_is_reserved_before_network_await(spool, lease):
    fake = Fake()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pending():
        entered.set()
        await release.wait()

    fake.on_download = pending
    first = asyncio.create_task(stage(spool, fake, lease))
    await asyncio.wait_for(entered.wait(), timeout=2)
    with pytest.raises(ArtifactTransportError, match="capacity"):
        await stage(spool, Fake(), lease)
    release.set()
    result = await first
    assert spool.revoke(result["handle"])


@pytest.mark.asyncio
async def test_cancelled_download_closes_fd_and_releases_reservation(spool, lease):
    fake = Fake()
    entered = asyncio.Event()

    async def pending():
        entered.set()
        await asyncio.Event().wait()

    fake.on_download = pending
    task = asyncio.create_task(stage(spool, fake, lease))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not spool._entries and spool._pending == 0
    staged = await stage(spool, Fake(), lease)
    assert spool.revoke(staged["handle"])


@pytest.mark.asyncio
async def test_inspection_timeout_closes_file(spool, lease):
    fake = Fake()
    result = await stage(spool, fake, lease)
    file = spool._entries[result["handle"]].file

    async def blocked(_):
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await spool.inspect_once(fake, result["handle"], blocked)
    assert file.closed
    assert not spool._entries


@pytest.mark.asyncio
async def test_manual_revocation_closes_private_descriptor(spool, lease):
    fake = Fake()
    staged = await stage(spool, fake, lease)
    fd = spool._entries[staged["handle"]].file
    assert spool.revoke(staged["handle"])
    assert fd.closed
    assert not spool.revoke(staged["handle"])


@pytest.mark.parametrize("name", ["private", "bad"])
def test_root_is_not_implicitly_provisioned(tmp_path, name):
    root = tmp_path / name
    with pytest.raises(ArtifactTransportError, match="not provisioned"):
        ArtifactSpool(tmp_path, root)
    assert not root.exists()


def test_symlink_root_and_permissive_root_fail_closed(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    with pytest.raises(ArtifactTransportError, match="unsafe permissions"):
        ArtifactSpool(tmp_path, tmp_path / "linked")
    os.chmod(real, 0o777)
    with pytest.raises(ArtifactTransportError, match="unsafe permissions"):
        ArtifactSpool(tmp_path, real)


def test_parent_is_tightly_scoped_and_rejects_symlink(tmp_path):
    parent = tmp_path / "trusted"
    parent.mkdir(mode=0o700)
    root = parent / "private"
    root.mkdir(mode=0o700)
    another = tmp_path / "another"
    another.mkdir(mode=0o700)
    with pytest.raises(ArtifactTransportError, match="approved child"):
        ArtifactSpool(another, root)
    os.chmod(parent, 0o777)
    with pytest.raises(ArtifactTransportError, match="unsafe permissions"):
        ArtifactSpool(tmp_path, parent)


def test_invalid_lease_and_spool_capacity_fail_closed(lease, spool):
    for mutation in [
        {"owner": "../../etc"}, {"run_attempt": True}, {"head_sha": "A" * 40},
        {"expected_sha256": "INVALID"}, {"artifact_id": 0},
    ]:
        from dataclasses import replace

        with pytest.raises(ArtifactTransportError):
            replace(lease, **mutation).validate()
    with pytest.raises(ArtifactTransportError, match="TTL"):
        ArtifactSpool(spool._root.parent, spool._root, ttl_s=0)
    with pytest.raises(ArtifactTransportError, match="capacity"):
        ArtifactSpool(spool._root.parent, spool._root, max_handles=9)

@pytest.mark.asyncio
async def test_internal_client_lies_about_on_disk_bytes_fail_closed(spool, lease):
    fake = Fake()

    async def dishonest(*args, **kwargs):
        output = args[3]
        output.write(b"x" * len(ZIP))
        return {"sha256": DIGEST, "size_bytes": len(ZIP), "entries": 1}

    fake.download_action_artifact_zip = dishonest
    with pytest.raises(ArtifactTransportError, match="inode checksum"):
        await stage(spool, fake, lease)
    assert not spool._entries and spool._pending == 0


@pytest.mark.asyncio
async def test_internal_client_false_directory_entry_count_fail_closed(spool, lease):
    fake = Fake()

    async def dishonest(*args, **kwargs):
        output = args[3]
        output.write(ZIP)
        return {"sha256": DIGEST, "size_bytes": len(ZIP), "entries": 999}

    fake.download_action_artifact_zip = dishonest
    with pytest.raises(ArtifactTransportError, match="ZIP directory metadata"):
        await stage(spool, fake, lease)
    assert not spool._entries


@pytest.mark.asyncio
async def test_internal_client_does_not_fill_expected_inode_fail_closed(spool, lease):
    fake = Fake()

    async def incomplete(*args, **kwargs):
        output = args[3]
        output.write(ZIP[:-2])
        return {"sha256": DIGEST, "size_bytes": len(ZIP), "entries": 1}

    fake.download_action_artifact_zip = incomplete
    with pytest.raises(ArtifactTransportError, match="inode size"):
        await stage(spool, fake, lease)
    assert not spool._entries
