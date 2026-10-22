"""Fail-closed rollout receipt tests (no Docker, no production credentials)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "deploy_state.py"
DEPLOY = ROOT / "scripts" / "deploy-from-registry.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
GW = "registry.test/gpakoh/agent-ssh-gateway@sha256:" + "a" * 64
MCP = "registry.test/gpakoh/mcp-server@sha256:" + "b" * 64
SSHD = "registry.test/gpakoh/ssh-gateway-sshd@sha256:" + "c" * 64
SHA = "d" * 40
FILENAME = "web-ssh-gateway-deploy.json"


def command(action: str, directory: Path, *params: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), action, str(directory), *params],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def private_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "trusted"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def test_private_state_write_is_atomic_and_readable(tmp_path: Path) -> None:
    directory = private_directory(tmp_path)
    assert command("preflight", directory).returncode == 0
    done = command("write", directory, GW, MCP, SSHD, SHA)
    assert done.returncode == 0, done.stderr
    receipt = directory / FILENAME
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert receipt.stat().st_uid == os.geteuid()
    doc = json.loads(receipt.read_text())
    assert doc["gateway_image"] == GW
    assert doc["deploy_sha"] == SHA
    assert set(doc) == {"gateway_image", "mcp_server_image", "sshd_image", "deploy_sha", "deployed_at"}
    assert command("read", directory, "mcp_server_image").stdout.strip() == MCP
    assert command("preflight", directory).returncode == 0
    assert {x.name for x in directory.iterdir()} == {FILENAME}


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o770])
def test_rejects_nonprivate_directory_before_writing(tmp_path: Path, mode: int) -> None:
    directory = private_directory(tmp_path)
    directory.chmod(mode)
    got = command("preflight", directory)
    assert got.returncode != 0
    assert "DEPLOY_STATE_UNTRUSTED" in got.stderr
    assert command("write", directory, GW, MCP, SSHD, SHA).returncode != 0
    assert not (directory / FILENAME).exists()


def test_world_writable_nonsticky_ancestor_is_untrusted(tmp_path: Path) -> None:
    parent = tmp_path / "public"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)
    nested = parent / "private"
    nested.mkdir(mode=0o700)
    assert command("preflight", nested).returncode != 0


def test_rejects_symlink_directory(tmp_path: Path) -> None:
    directory = private_directory(tmp_path)
    symlink = tmp_path / "redirected"
    symlink.symlink_to(directory, target_is_directory=True)
    assert command("preflight", symlink).returncode != 0


def test_legacy_world_writable_receipt_blocks_reads_and_rewrites(tmp_path: Path) -> None:
    directory = private_directory(tmp_path)
    receipt = directory / FILENAME
    receipt.write_text("not trusted")
    receipt.chmod(0o666)
    assert command("preflight", directory).returncode != 0
    assert command("read", directory, "gateway_image").returncode != 0
    assert command("write", directory, GW, MCP, SSHD, SHA).returncode != 0
    assert receipt.read_text() == "not trusted"


def test_receipt_symlink_and_hardlink_refused(tmp_path: Path) -> None:
    directory = private_directory(tmp_path)
    foreign = tmp_path / "other"
    foreign.write_text("{}")
    (directory / FILENAME).symlink_to(foreign)
    assert command("preflight", directory).returncode != 0
    (directory / FILENAME).unlink()
    foreign.unlink()
    foreign.write_text("{}", encoding="utf-8")
    foreign.chmod(0o600)
    os.link(foreign, directory / FILENAME)
    assert command("preflight", directory).returncode != 0


@pytest.mark.parametrize("contents", ['{broken', "[]", "{}", '{"gateway_image": 12}', "x" * 5000])
def test_corrupt_receipt_is_never_silently_treated_as_empty(
    tmp_path: Path, contents: str
) -> None:
    directory = private_directory(tmp_path)
    receipt = directory / FILENAME
    receipt.write_text(contents)
    receipt.chmod(0o600)
    assert command("preflight", directory).returncode != 0
    assert command("read", directory, "sshd_image").returncode != 0


def test_rejects_unknown_field_and_nonexistent_state_directory(tmp_path: Path) -> None:
    directory = private_directory(tmp_path)
    assert command("read", directory, "__class__").returncode != 0
    assert command("preflight", tmp_path / "missing").returncode != 0


def test_existing_verified_receipt_can_be_replaced_atomically(tmp_path: Path) -> None:
    directory = private_directory(tmp_path)
    assert command("write", directory, GW, MCP, SSHD, SHA).returncode == 0
    next_sha = "e" * 40
    assert command("write", directory, GW, MCP, SSHD, next_sha).returncode == 0
    assert command("read", directory, "deploy_sha").stdout.strip() == next_sha
    assert stat.S_IMODE((directory / FILENAME).stat().st_mode) == 0o600


def test_atomic_replace_failure_preserves_verified_previous_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import deploy_state

    directory = private_directory(tmp_path)
    assert command("write", directory, GW, MCP, SSHD, SHA).returncode == 0
    original = (directory / FILENAME).read_bytes()
    fd = deploy_state._directory(str(directory))

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(deploy_state.os, "replace", fail_replace)
    try:
        with pytest.raises(OSError, match="simulated rename failure"):
            deploy_state._write(
                fd,
                {
                    "gateway_image": GW,
                    "mcp_server_image": MCP,
                    "sshd_image": SSHD,
                    "deploy_sha": "e" * 40,
                    "deployed_at": "2026-10-22T00:00:00+00:00",
                },
            )
    finally:
        os.close(fd)
    assert (directory / FILENAME).read_bytes() == original
    assert {p.name for p in directory.iterdir()} == {FILENAME}


def test_post_commit_fsync_failure_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import deploy_state

    directory = private_directory(tmp_path)
    fd = deploy_state._directory(str(directory))
    fsync = deploy_state.os.fsync

    def fail_dir_fsync(descriptor: int) -> None:
        if descriptor == fd:
            raise OSError("simulated directory fsync failure")
        fsync(descriptor)

    monkeypatch.setattr(deploy_state.os, "fsync", fail_dir_fsync)
    try:
        with pytest.raises(OSError, match="simulated directory fsync failure"):
            deploy_state._write(
                fd,
                {
                    "gateway_image": GW,
                    "mcp_server_image": MCP,
                    "sshd_image": SSHD,
                    "deploy_sha": SHA,
                    "deployed_at": "2026-10-22T00:00:00+00:00",
                },
            )
    finally:
        os.close(fd)
    # A post-rename fsync failure has an unknown durable outcome. No silent
    # success is allowed even though the new path may now exist.
    assert (directory / FILENAME).exists()


def test_workflow_requires_private_volume_and_script_preflight() -> None:
    script = DEPLOY.read_text()
    workflow = WORKFLOW.read_text()
    assert 'python3 "$ROOT/scripts/deploy_state.py" preflight "$STATE_DIR"' in script
    assert script.index('preflight "$STATE_DIR"') < script.index('docker pull "$GATEWAY_REPO:$DEPLOY_TAG"')
    assert 'python3 "$ROOT/scripts/deploy_state.py" read "$STATE_DIR" "$1"' in script
    assert 'python3 "$ROOT/scripts/deploy_state.py" write "$STATE_DIR" "$1" "$2" "$3" "$DEPLOY_TAG"' in script
    assert 'chmod 777 "$STATE_DIR"' not in script
    assert 'chmod 666 "$STATE_FILE"' not in script
    assert "ssh-gateway-deploy-state:/deploy/web-ssh-gateway-state" in workflow
    assert "DEPLOY_STATE_DIR: /deploy/web-ssh-gateway-state" in workflow
    assert "web-ssh-gateway/.state:/deploy/" not in workflow
    assert "ln -s /deploy/web-ssh-gateway-state .state" not in workflow
