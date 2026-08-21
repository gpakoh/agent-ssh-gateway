"""Regression suite for the incomplete-history / shallow-bundle false-pass finding.

Contract under test (architect acceptance target):

    Given   a shallow source clone / missing parent objects
    When    managed source publication attempts to create the artifact
    Then    the artifact MUST NOT be accepted as publishable/consumable
    And     any failure happens before an OpenCode worker launches

For a valid full source repository the published bundle must survive the
full evidence chain: publish -> bundle verify -> scratch clone -> detached
checkout at the exact expected base_ref.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server import agent_sources
from examples.mcp_server.agent_paths import managed_source_bundle_path
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    _assert_bundle_usable,
)
from examples.mcp_server.agent_tools import _build_opencode_script

TASK_ID = "c12345678901"


class _RegistryStub:
    def __init__(self, root: Path):
        self._root = root

    def project_info(self, project: str) -> dict[str, str]:
        return {"root": str(self._root)}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _init_history_repo(root: Path, commits: int = 5) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Completeness Tests")
    for i in range(commits):
        (root / f"f{i}.txt").write_text(f"c{i}\n", encoding="utf-8")
        _git(root, "add", f"f{i}.txt")
        _git(root, "commit", "-q", "-m", f"c{i}")
    return _git(root, "rev-parse", "HEAD")


def _make_shallow_clone(source: Path, destination: Path, depth: int) -> str:
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth",
            str(depth),
            f"file://{source}",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return _git(destination, "rev-parse", "HEAD")


def _craft_prerequisite_bundle(source: Path, destination: Path) -> str:
    """Build the demonstrated false-pass artifact: single head equals the
    repository tip, yet cloning requires prerequisite objects consumers do
    not have."""
    tip = _git(source, "rev-parse", "HEAD")
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "bundle",
            "create",
            str(destination),
            "^HEAD~4",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    heads = subprocess.run(
        ["git", "bundle", "list-heads", str(destination)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert heads[0] == tip
    return tip


@pytest.fixture()
def managed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source_root = tmp_path / "source-root"
    source_root.mkdir()

    def install(project_source: Path) -> None:
        monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
        monkeypatch.setattr(
            agent_sources,
            "get_registry",
            lambda: _RegistryStub(project_source),
        )

    return install


def _published_bundles(source_root: Path) -> list[Path]:
    return list(source_root.rglob("*.bundle"))


def test_shallow_depth1_source_is_rejected(tmp_path, managed_env):
    origin = tmp_path / "origin"
    head = _init_history_repo(origin)
    shallow = tmp_path / "shallow1"
    assert _make_shallow_clone(origin, shallow, depth=1) == head

    managed_env(shallow)
    with pytest.raises(ManagedSourceBundleError):
        agent_sources.ensure_managed_source_bundle("proj", head)

    assert _published_bundles(tmp_path / "source-root") == []


def test_shallow_depth2_source_is_rejected(tmp_path, managed_env):
    origin = tmp_path / "origin2"
    head = _init_history_repo(origin)
    shallow = tmp_path / "shallow2"
    assert _make_shallow_clone(origin, shallow, depth=2) == head

    managed_env(shallow)
    with pytest.raises(ManagedSourceBundleError):
        agent_sources.ensure_managed_source_bundle("proj", head)

    assert _published_bundles(tmp_path / "source-root") == []


def test_full_source_publishes_and_clones_to_expected_commit(tmp_path, managed_env):
    origin = tmp_path / "origin-full"
    head = _init_history_repo(origin)

    managed_env(origin)
    bundle = agent_sources.ensure_managed_source_bundle("proj", head)
    assert bundle is not None

    scratch = tmp_path / "acceptance-clone"
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", bundle, str(scratch)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(scratch, "checkout", "-q", "--detach", head)
    assert _git(scratch, "rev-parse", "HEAD") == head


def test_prerequisite_bundle_fails_usability_gate(tmp_path):
    origin = tmp_path / "origin-prereq"
    head = _init_history_repo(origin)
    planted = tmp_path / "planted.bundle"
    _craft_prerequisite_bundle(origin, planted)

    with pytest.raises(ManagedSourceBundleError):
        _assert_bundle_usable(planted, head, full_proof=False)
    with pytest.raises(ManagedSourceBundleError):
        _assert_bundle_usable(planted, head, full_proof=True)


def test_valid_bundle_passes_full_proof(tmp_path):
    origin = tmp_path / "origin-valid"
    head = _init_history_repo(origin)
    bundle = tmp_path / "valid.bundle"
    _git(origin, "bundle", "create", str(bundle), "HEAD")

    _assert_bundle_usable(bundle, head, full_proof=False)
    _assert_bundle_usable(bundle, head, full_proof=True)


def test_planted_broken_bundle_at_managed_path_is_not_returned(tmp_path, managed_env):
    origin = tmp_path / "origin-plant"
    head = _init_history_repo(origin)
    managed_env(origin)
    target = Path(managed_source_bundle_path("proj", head))
    target.parent.mkdir(parents=True, exist_ok=True)
    _craft_prerequisite_bundle(origin, target)
    broken_bytes = target.read_bytes()

    bundle = agent_sources.ensure_managed_source_bundle("proj", head)
    assert bundle is not None
    healed = Path(bundle).read_bytes()
    assert healed != broken_bytes

    _assert_bundle_usable(Path(bundle), head, full_proof=True)


def test_generated_script_verifies_bundle_before_clone():
    origin = Path("/nonexistent-for-static-check")
    script = _build_opencode_script(
        "/tmp/absolute-td",
        TASK_ID,
        None,
        worktree_path="/tmp/ws",
        base_ref="a" * 40,
        managed_clone=True,
        managed_source_path="/tmp/sources/proj.bundle",
    )
    lines = script.splitlines()
    verify_idx = next(i for i, line in enumerate(lines) if "bundle verify" in line)
    clone_idx = next(
        i
        for i, line in enumerate(lines)
        if 'git clone --no-hardlinks --no-checkout "$MANAGED_SOURCE_BUNDLE"' in line
    )
    assert verify_idx < clone_idx
    assert origin.exists() is False


def test_consumer_rejects_broken_bundle_before_worker_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "source-root"))

    origin = tmp_path / "origin-e2e"
    head = _init_history_repo(origin)
    bundle_path = Path(managed_source_bundle_path("proj-e2e", head))
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    _craft_prerequisite_bundle(origin, bundle_path)

    artifacts = tmp_path / "artifacts" / TASK_ID
    artifacts.mkdir(parents=True)
    workspace = tmp_path / "workspaces" / TASK_ID
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "opencode-launched"
    fake = fake_bin / "opencode"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(
        str(artifacts),
        TASK_ID,
        None,
        worktree_path=str(workspace),
        base_ref=head,
        managed_clone=True,
        managed_source_path=str(bundle_path),
    )
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=origin,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 73, result.stderr or result.stdout
    status = (artifacts / "agent-status.md").read_text(encoding="utf-8")
    assert "failed integrity verification" in status
    assert not marker.exists(), "worker must never launch on a broken bundle"
