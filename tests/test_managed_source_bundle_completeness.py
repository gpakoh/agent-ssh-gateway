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

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server import agent_sources
from examples.mcp_server.agent_paths import managed_source_bundle_path, project_state_key
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    _assert_bundle_usable,
)
from examples.mcp_server.agent_tasks import build_task_json
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
        ["git", "clone", "-q", "--no-hardlinks", bundle.path, str(scratch)],
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
    healed = Path(bundle.path).read_bytes()
    assert healed != broken_bytes

    _assert_bundle_usable(Path(bundle.path), head, full_proof=True)


def test_generated_script_verifies_bundle_before_clone():
    script = _build_opencode_script(
        "/tmp/absolute-td",
        TASK_ID,
        None,
        worktree_path="/tmp/ws",
        base_ref="a" * 40,
        managed_clone=True,
        managed_source_path="/tmp/sources/proj.bundle",
        managed_source_sha256="b" * 64,
    )
    lines = script.splitlines()
    expected_idx = next(
        i for i, line in enumerate(lines) if "MANAGED_SOURCE_EXPECTED_SHA256=" in line
    )
    copy_idx = next(i for i, line in enumerate(lines) if "MANAGED_COPY_PY" in line)
    verify_idx = next(i for i, line in enumerate(lines) if "bundle verify" in line)
    clone_idx = next(
        i
        for i, line in enumerate(lines)
        if 'git clone --no-hardlinks --no-checkout "$MANAGED_SOURCE_COPY"' in line
    )
    assert expected_idx < copy_idx < verify_idx < clone_idx
    # Every git operation must consume ONLY the verified private copy; the
    # mutable published path may appear solely as the copy source argument.
    assert 'git bundle list-heads "$MANAGED_SOURCE_BUNDLE"' not in script
    assert 'git bundle list-heads "$MANAGED_SOURCE_COPY"' in script
    assert 'bundle verify "$MANAGED_SOURCE_BUNDLE"' not in script
    assert 'bundle verify "$MANAGED_SOURCE_COPY"' in script
    assert '--no-checkout "$MANAGED_SOURCE_BUNDLE"' not in script
    heads_idx = next(i for i, line in enumerate(lines) if "list-heads" in line)
    assert expected_idx < copy_idx < heads_idx < verify_idx < clone_idx


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

    planted_digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    script = _build_opencode_script(
        str(artifacts),
        TASK_ID,
        None,
        worktree_path=str(workspace),
        base_ref=head,
        managed_clone=True,
        managed_source_path=str(bundle_path),
        managed_source_sha256=planted_digest,
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


# ---------------------------------------------------------------------------
# Digest-binding adversarial suite (TOCTOU between verification and clone).
#
# Contract under test:
#   The worker must consume a private copy of the managed bundle whose bytes
#   hash to the supervisor-captured SHA-256, and every git operation
#   (list-heads / verify / clone) must run against that private copy.
# ---------------------------------------------------------------------------


def _craft_attacker_bundle(destination: Path) -> str:
    """A *valid* bundle of an unrelated repository (single head != base_ref)."""
    repo = destination.parent / "attacker-src"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "attacker@example.invalid")
    _git(repo, "config", "user.name", "Attacker")
    (repo / "evil.txt").write_text("attacker content\n", encoding="utf-8")
    _git(repo, "add", "evil.txt")
    _git(repo, "commit", "-q", "-m", "attacker tip")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "bundle", "create", str(destination), "HEAD")
    return head


def _publish_valid_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    source_root = tmp_path / "source-root"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    origin = tmp_path / "origin-bind"
    head = _init_history_repo(origin)
    target = Path(managed_source_bundle_path("proj-bind", head))
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(origin), "bundle", "create", str(target), "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert agent_sources.capture_bundle_digest(target) == digest
    return head, digest


def test_a_publication_digest_binds_final_artifact(tmp_path, monkeypatch):
    """(A) Valid artifact: captured digest == sha256(final bytes); the private
    verified copy clones to the exact expected commit."""
    head, digest = _publish_valid_bundle(tmp_path, monkeypatch)
    target = Path(managed_source_bundle_path("proj-bind", head))

    copies_dir = tmp_path / "copies"
    copy = agent_sources.secure_copy_and_verify(target, digest, dest_dir=copies_dir)
    assert copy.is_file()
    assert oct(copy.stat().st_mode & 0o777) == "0o400"

    scratch = tmp_path / "acceptance"
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(copy), str(scratch)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(scratch, "checkout", "-q", "--detach", head)
    assert _git(scratch, "rev-parse", "HEAD") == head


def test_b_worker_rejects_bundle_swapped_before_launch(tmp_path, monkeypatch):
    """(B) SOURCE_ROOT replaced with a different valid bundle before the
    worker starts: digest mismatch must fail closed BEFORE OpenCode."""
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)

    head, supervisor_digest = _publish_valid_bundle(tmp_path, monkeypatch)
    target = Path(managed_source_bundle_path("proj-bind", head))
    attacker_bytes = target.read_bytes()
    _craft_attacker_bundle(target)
    assert target.read_bytes() != attacker_bytes

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
        managed_source_path=str(target),
        managed_source_sha256=supervisor_digest,
    )
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 73, result.stderr or result.stdout
    status = (artifacts / "agent-status.md").read_text(encoding="utf-8")
    assert "digest mismatch" in status
    assert not marker.exists(), "worker must never launch on swapped bytes"
    assert not workspace.exists()


def test_c_private_copy_survives_replacement_after_verification(tmp_path, monkeypatch):
    """(C) SOURCE_ROOT replaced after the worker's verified copy: all git
    operations continue on the private copy and stay bound to base_ref."""
    head, digest = _publish_valid_bundle(tmp_path, monkeypatch)
    target = Path(managed_source_bundle_path("proj-bind", head))

    copies_dir = tmp_path / "copies-late"
    copy = agent_sources.secure_copy_and_verify(target, digest, dest_dir=copies_dir)
    trusted_bytes = copy.read_bytes()

    _craft_attacker_bundle(target)
    assert target.read_bytes() != trusted_bytes
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == digest

    bare = tmp_path / "verify-bare.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(bare), "bundle", "verify", str(copy)],
        check=True,
        capture_output=True,
        text=True,
    )
    scratch = tmp_path / "late-clone"
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(copy), str(scratch)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(scratch, "checkout", "-q", "--detach", head)
    assert _git(scratch, "rev-parse", "HEAD") == head


def test_d_tampered_artifact_fails_closed_and_cleans_up(tmp_path, monkeypatch):
    """(D) Corrupted bytes: digest mismatch rejected before any git use and
    no private-copy leftovers survive."""
    head, digest = _publish_valid_bundle(tmp_path, monkeypatch)
    target = Path(managed_source_bundle_path("proj-bind", head))

    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))

    copies_dir = tmp_path / "copies-tamper"
    with pytest.raises(agent_sources.ManagedSourceDigestError):
        agent_sources.secure_copy_and_verify(target, digest, dest_dir=copies_dir)

    assert list(copies_dir.iterdir()) == [], (
        "failed verification must not leave private copies behind"
    )


_MALFORMED_DIGESTS = [
    "",
    "z" * 64,
    "a" * 63,
    "a" * 65,
    "A" * 64,
]


def test_e1_malformed_digest_rejected_by_metadata_and_builder():
    """(E1) Non-hex / wrong-length digests fail closed in task metadata AND
    at script build time -- nothing reaches a worker."""
    for bad in [*_MALFORMED_DIGESTS, 12345]:
        if isinstance(bad, str):
            with pytest.raises(ValueError):
                build_task_json(
                    task_id=TASK_ID,
                    agent="opencode",
                    base_ref="b" * 40,
                    managed_source_sha256=bad,
                )
        with pytest.raises(ValueError):
            _build_opencode_script(
                "/tmp/absolute-td",
                TASK_ID,
                None,
                worktree_path="/tmp/ws",
                base_ref="a" * 40,
                managed_clone=True,
                managed_source_path="/tmp/sources/proj.bundle",
                managed_source_sha256=bad,
            )


def test_e2_missing_digest_fails_closed_before_worker_launch(tmp_path):
    """(E2) Missing digest metadata in managed mode MUST fail closed before
    worker launch. There is no supervisor-time recapture fallback."""
    missing_artifact = tmp_path / "nope.bundle"

    with pytest.raises(ValueError):
        _build_opencode_script(
            "/tmp/absolute-td",
            TASK_ID,
            None,
            worktree_path="/tmp/ws",
            base_ref="a" * 40,
            managed_clone=True,
            managed_source_path=str(missing_artifact),
        )
    with pytest.raises(ValueError):
        _build_opencode_script(
            "/tmp/absolute-td",
            TASK_ID,
            None,
            worktree_path="/tmp/ws",
            base_ref="a" * 40,
            managed_clone=True,
            managed_source_path=str(missing_artifact),
            managed_source_sha256="   ",
        )


def test_e3_builder_never_touches_the_artifact_path():
    """(E3) Digest comes exclusively from trusted task metadata: script
    building must not read/recapture the mutable published file."""
    script = _build_opencode_script(
        "/tmp/absolute-td",
        TASK_ID,
        None,
        worktree_path="/tmp/ws",
        base_ref="a" * 40,
        managed_clone=True,
        managed_source_path="/nonexistent-must-not-be-read/proj.bundle",
        managed_source_sha256="b" * 64,
    )
    assert "MANAGED_SOURCE_EXPECTED_SHA256='" + "b" * 64 + "'" in script


def test_f_task_metadata_channel_carries_publisher_digest(tmp_path, monkeypatch):
    """The digest written by the control plane into task.json is the same
    value the launch site extracts and embeds into the worker script."""
    from examples.mcp_server.agent_tasks import write_agent_task

    head, digest = _publish_valid_bundle(tmp_path, monkeypatch)

    def _run_script(_project: str, script: str):
        proc = subprocess.run(
            ["sh", "-c", script],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    write_agent_task(
        _run_script,
        project="proj-meta",
        task_id=TASK_ID,
        agent="opencode",
        task="meta channel check",
        worktree_path=str(tmp_path / "ws"),
        base_ref=head,
        allowed_backends=["opencode"],
        managed_source_sha256=digest,
    )
    stored = (tmp_path / ".ai-bridge" / "tasks" / TASK_ID / "task.json").read_text(encoding="utf-8")
    assert f'"managed_source_sha256": "{digest}"' in stored

    import json as _json

    task_json = _json.loads(stored)
    raw = task_json.get("managed_source_sha256")
    embedded_digest = raw.strip() if isinstance(raw, str) and raw.strip() else None
    assert embedded_digest == digest

    workspace = tmp_path / "ws"
    script = _build_opencode_script(
        str(tmp_path / ".ai-bridge" / "tasks" / TASK_ID),
        TASK_ID,
        None,
        worktree_path=str(workspace),
        base_ref=head,
        managed_clone=True,
        managed_source_path=str(managed_source_bundle_path("proj-meta", head)),
        managed_source_sha256=embedded_digest,
    )
    assert f"MANAGED_SOURCE_EXPECTED_SHA256='{digest}'" in script


def test_g_publication_reuse_rebinds_identical_digest(tmp_path, managed_env):
    """(F) ensure() binds the digest over proven snapshot bytes; a reuse
    call re-proves and rebinds deterministically to the same value."""
    origin = tmp_path / "origin-rebind"
    head = _init_history_repo(origin)
    managed_env(origin)

    first = agent_sources.ensure_managed_source_bundle("proj-rebind", head)
    assert first is not None
    assert hashlib.sha256(Path(first.path).read_bytes()).hexdigest() == first.sha256

    second = agent_sources.ensure_managed_source_bundle("proj-rebind", head)
    assert second is not None
    assert second.path == first.path
    assert second.sha256 == first.sha256


# ---------------------------------------------------------------------------
# FINAL VERIFICATION ROUND additions (architect items 2, 3, 6).
# ---------------------------------------------------------------------------


def _craft_same_head_variant(source: Path, destination: Path) -> None:
    """Bundle the SAME commit into byte-different artifacts.

    Bundle format v3 changes the artifact header deterministically while
    preserving the single advertised HEAD required by managed-source policy.
    This does not depend on repack implementation details or ambient packing.
    """
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "bundle",
            "create",
            "--version=3",
            str(destination),
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_h1_same_head_different_bytes_swap_rejected_before_worker_copy(tmp_path, monkeypatch):
    """Architect scenario 2A–E: valid bundle A published; control plane
    binds digest over its proven snapshot; path replaced by valid bundle B
    advertising the SAME commit but different bytes; the worker must get a
    digest mismatch against the trusted metadata."""
    source_root = tmp_path / "source-root"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    origin = tmp_path / "origin-h"
    head = _init_history_repo(origin)

    target = Path(managed_source_bundle_path("proj-h", head))
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "bundle",
            "create",
            "--version=2",
            str(target),
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    bytes_a = target.read_bytes()
    sha_a = hashlib.sha256(bytes_a).hexdigest()

    publication = agent_sources.bind_publication_bytes(target, head)
    assert publication.sha256 == sha_a

    variant = tmp_path / "variant-b.bundle"
    _craft_same_head_variant(origin, variant)
    bytes_b = variant.read_bytes()
    assert bytes_b != bytes_a, "same-commit v2/v3 bundles must differ in bytes"
    heads_b = subprocess.run(
        ["git", "bundle", "list-heads", str(variant)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert heads_b[0] == head, "attacker bundle must advertise the same commit"
    subprocess.run(
        ["git", "bundle", "verify", str(variant)],
        cwd=origin,
        check=True,
        capture_output=True,
    )

    target.write_bytes(bytes_b)

    with pytest.raises(agent_sources.ManagedSourceDigestError, match="digest mismatch"):
        agent_sources.secure_copy_and_verify(target, publication.sha256)

    rebound = agent_sources.bind_publication_bytes(target, head)
    assert rebound.sha256 != publication.sha256


def test_h2_publication_digest_immune_to_post_snapshot_replacement(tmp_path, monkeypatch):
    """Replacement AFTER the control-plane snapshot cannot change the bound
    digest/proof pair already handed to trusted metadata."""
    source_root = tmp_path / "source-root"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    origin = tmp_path / "origin-h2"
    head = _init_history_repo(origin)

    target = Path(managed_source_bundle_path("proj-h2", head))
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "bundle",
            "create",
            "--version=2",
            str(target),
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    bytes_a = target.read_bytes()
    sha_a = hashlib.sha256(bytes_a).hexdigest()

    publication = agent_sources.bind_publication_bytes(target, head)
    assert publication.sha256 == sha_a

    variant = tmp_path / "h2-variant.bundle"
    _craft_same_head_variant(origin, variant)
    target.write_bytes(variant.read_bytes())

    assert publication.sha256 == sha_a


def test_i_digest_derives_from_snapshot_not_path_reread(tmp_path, monkeypatch):
    """The bound digest MUST come from the private snapshot bytes that the
    proof ran on -- never from a separate re-open of the mutable path."""
    source_root = tmp_path / "source-root"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    origin = tmp_path / "origin-i"
    head = _init_history_repo(origin)

    target = Path(managed_source_bundle_path("proj-i", head))
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(origin), "bundle", "create", str(target), "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    real_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    monkeypatch.setattr(agent_sources, "capture_bundle_digest", lambda _path: "f" * 64)
    publication = agent_sources.bind_publication_bytes(target, head)
    assert publication.sha256 == real_sha
    assert publication.sha256 != "f" * 64


def test_j_opencode_entrypoint_propagates_metadata_digest(tmp_path, monkeypatch):
    """Second managed launch entrypoint (project_run_opencode): the digest
    stored in task.json is embedded into the generated worker script."""
    from unittest.mock import MagicMock as _MagicMock

    from examples.mcp_server.opencode_tools import project_run_opencode

    state_root = tmp_path / "state"
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(state_root))
    monkeypatch.setenv("MCP_AGENT_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "source-root"))
    monkeypatch.setattr(
        "examples.mcp_server.opencode_tools._resolve_project_root",
        lambda _project: None,
    )

    base_ref = "b" * 40
    # task_dir() is absolute only when STATE_ROOT is set; compute the correct
    # absolute path and place fixtures there (not in .ai-bridge/).
    project_key = project_state_key("p")
    absolute_task_dir = state_root / project_key / "tasks" / TASK_ID
    absolute_task_dir.mkdir(parents=True)
    (absolute_task_dir / "current-plan.md").write_text(
        "# plan\n\nverification round\n", encoding="utf-8"
    )
    (absolute_task_dir / "task.json").write_text(
        build_task_json(
            task_id=TASK_ID,
            agent="opencode",
            base_ref=base_ref,
            managed_source_sha256="c" * 64,
        ),
        encoding="utf-8",
    )
    # managed_source_bundle_path resolution requires a bundle at the expected path.
    bundle_path = Path(managed_source_bundle_path("p", base_ref))
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    origin = tmp_path / "origin-opencode"
    _init_history_repo(origin)
    subprocess.run(
        ["git", "-C", str(origin), "bundle", "create", str(bundle_path), "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )

    def run_command(_project: str, command: str):
        completed = subprocess.run(
            ["sh", "-c", command],
            cwd=str(tmp_path),
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
        }

    run_script = _MagicMock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})
    result = project_run_opencode(
        run_command,
        project="p",
        task_id=TASK_ID,
        run_script=run_script,
    )

    assert result["status"] != "error", result.get("error")
    script = run_script.call_args[0][1]
    assert "MANAGED_SOURCE_EXPECTED_SHA256='" + "c" * 64 + "'" in script
