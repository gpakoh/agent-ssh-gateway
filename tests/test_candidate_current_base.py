"""Regressions for #699: symbolic bases and the actual clone-origin layouts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server import candidate_clone as clones
from examples.mcp_server.agent_sources import ManagedSourcePublication
from tests.test_candidate_clone import _git, _idle_guard
from tests.test_candidate_clone import registry_fixture as registry_fixture
from tests.test_candidate_fetch import assert_preserved, fetch, run
from tests.test_candidate_fetch import commit as commit_fetch
from tests.test_candidate_fetch import managed as managed


@pytest.mark.parametrize("ref", ["origin/main", "refs/heads/origin/main"])
def test_generic_probe_preserves_literal_origin_branch_names(tmp_path, monkeypatch, ref):
    monkeypatch.setattr(
        clones,
        "_resolve_trusted_remote",
        lambda _: ("test", "https://git.example.test/owner/repo.git", "secret"),
    )
    calls = []

    def remote(argv, **kwargs):
        calls.append(argv)
        expected = "refs/heads/origin/main"
        if expected in argv:
            return subprocess.CompletedProcess(argv, 0, "a" * 40 + "\t" + expected + "\n", "")
        return subprocess.CompletedProcess(argv, 2, "", "")

    monkeypatch.setattr(clones.subprocess, "run", remote)
    result = clones._probe_remote_ref(tmp_path, ref)
    assert result == clones.RemoteRefProbe(clones.RemoteRefStatus.FOUND, "a" * 40)
    assert "refs/heads/origin/main" in calls[0]


@pytest.mark.parametrize("status", [clones.RemoteRefStatus.ABSENT, clones.RemoteRefStatus.UNKNOWN])
@pytest.mark.parametrize("ref", ["origin/main", "refs/remotes/origin/main"])
def test_tracking_base_never_falls_back_to_stale_local_ref(
    registry_fixture, monkeypatch, status, ref
):
    workspace, source, config, journal, base = registry_fixture
    _git(source, "remote", "add", "origin", "https://git.example.test/owner/repo.git")
    _git(source, "update-ref", "refs/remotes/origin/main", base)
    monkeypatch.setattr(clones, "_probe_remote_ref", lambda *_: clones.RemoteRefProbe(status))
    with pytest.raises(clones.CandidateCloneError) as caught:
        clones.prepare_candidate_clone(
            "source-project", "fix/current-base", ref, config_dir=config, journal_root=journal
        )
    assert caught.value.code == (
        "SOURCE_REF_NOT_AVAILABLE"
        if status is clones.RemoteRefStatus.ABSENT
        else "SOURCE_REMOTE_STATE_UNKNOWN"
    )
    candidates = workspace / ".mcp-candidate-clones"
    assert not candidates.exists() or not list(candidates.iterdir())
    assert _git(source, "rev-parse", "HEAD") == base


def test_symbolic_candidate_can_be_abandoned_using_its_resolved_base(registry_fixture, monkeypatch):
    workspace, source, config, journal, base = registry_fixture
    _git(source, "branch", "named-base", base)
    branch = "fix/symbolic-cleanup"
    receipt = clones.prepare_candidate_clone(
        "source-project", branch, "named-base", config_dir=config, journal_root=journal
    )
    root = workspace / ".mcp-candidate-clones" / receipt.project_id
    # The mutable source ref may advance; the recorded immutable base must win.
    (source / "new.txt").write_text("new source commit")
    _git(source, "add", "new.txt")
    _git(source, "commit", "-qm", "advance source")
    _git(source, "branch", "-f", "named-base", "HEAD")
    monkeypatch.setattr(
        clones, "_probe_remote_ref", lambda *_: clones.RemoteRefProbe(clones.RemoteRefStatus.ABSENT)
    )
    args = dict(
        project_id=receipt.project_id,
        expected_head_sha=base,
        expected_branch=branch,
        expected_source_project="source-project",
        preserved_ref=None,
        abandon_unmodified=True,
        config_dir=config,
        journal_root=journal,
        reference_guard=_idle_guard,
    )
    result = clones.candidate_cleanup(**args)
    assert result.directory_removed and result.registry_removed
    assert not root.exists()
    assert clones.candidate_cleanup(**args).already_cleaned


def test_clone_records_its_actual_origin_at_creation(registry_fixture):
    workspace, _, config, journal, base = registry_fixture
    receipt = clones.prepare_candidate_clone(
        "source-project", "fix/origin-proof", base, config_dir=config, journal_root=journal
    )
    root = workspace / ".mcp-candidate-clones" / receipt.project_id
    metadata = json.loads((root / ".git/mcp-candidate-clone.json").read_text())
    assert metadata["clone_origin"] == _git(root, "remote", "get-url", "origin")
    assert metadata["base_sha"] == metadata["head"] == base
    assert not Path(metadata["clone_origin"]).exists()  # staging has been removed


def test_recovery_does_not_invent_origin_provenance(registry_fixture):
    workspace, _, config, journal, base = registry_fixture
    args = dict(
        project="source-project",
        branch="fix/legacy-recovery",
        base_ref=base,
        config_dir=config,
        journal_root=journal,
    )
    first = clones.prepare_candidate_clone(**args)
    root = workspace / ".mcp-candidate-clones" / first.project_id
    path = root / ".git/mcp-candidate-clone.json"
    metadata = json.loads(path.read_text())
    metadata.pop("clone_origin")
    path.write_text(json.dumps(metadata))
    assert clones.prepare_candidate_clone(**args).recovered
    assert "clone_origin" not in json.loads(path.read_text())


@pytest.mark.parametrize("ref", ["origin/main", "refs/remotes/origin/main"])
def test_prepare_tracking_base_materializes_current_remote_without_source_fetch(
    registry_fixture, monkeypatch, tmp_path, ref
):
    workspace, source, config, journal, base = registry_fixture
    remote = tmp_path / "remote"
    _git(tmp_path, "clone", "-q", str(source), str(remote))
    target = commit_fetch(remote, "current.txt", "fresh remote")
    _git(source, "remote", "add", "origin", "https://git.example.test/owner/repo.git")
    _git(source, "update-ref", "refs/remotes/origin/main", base)
    bundle = tmp_path / "remote.bundle"
    _git(remote, "bundle", "create", str(bundle), "HEAD")
    monkeypatch.setattr(
        clones,
        "_resolve_trusted_remote",
        lambda _: ("test", "https://git.example.test/owner/repo.git", "secret"),
    )
    original = clones.subprocess.run

    def commands(argv, **kwargs):
        if argv[:2] == ["git", "ls-remote"]:
            expected = "refs/heads/main"
            assert argv[-1] == expected
            return subprocess.CompletedProcess(argv, 0, target + "\t" + expected + "\n", "")
        return original(argv, **kwargs)

    monkeypatch.setattr(clones.subprocess, "run", commands)

    def publication(_project, sha):
        assert sha == target
        return ManagedSourcePublication(str(bundle), "a" * 64)

    monkeypatch.setattr(clones, "ensure_managed_source_bundle", publication)
    before = (source / ".git/index").read_bytes()
    fetch_head = source / ".git/FETCH_HEAD"
    fetch_head.write_text("operator-owned sentinel")
    fetch_head.chmod(0o444)
    receipt = clones.prepare_candidate_clone(
        "source-project", "fix/fresh-tracking", ref, config_dir=config, journal_root=journal
    )
    root = workspace / ".mcp-candidate-clones" / receipt.project_id
    assert receipt.base_sha == receipt.head == target
    assert (root / "current.txt").read_text() == "fresh remote"
    assert _git(source, "rev-parse", "refs/remotes/origin/main") == base
    assert fetch_head.read_text() == "operator-owned sentinel"
    assert (source / ".git/index").read_bytes() == before


def test_factory_clone_can_import_with_its_real_creation_receipt(managed, monkeypatch):
    m = managed
    config = m.source.parent.parent
    monkeypatch.setattr(
        clones,
        "_probe_remote_ref",
        lambda *_: clones.RemoteRefProbe(clones.RemoteRefStatus.FOUND, m.base),
    )
    branch = "fix/factory-to-fetch"
    receipt = clones.prepare_candidate_clone(
        "source-project", branch, m.base, config_dir=config, journal_root=config / "journals"
    )
    root = m.source.parent / ".mcp-candidate-clones" / receipt.project_id
    from app.workspace.registry import get_registry

    get_registry(config / "projects.yaml")
    head = commit_fetch(root, "new-feature.txt", "preserve feature")
    run(m.upstream, "fetch", "-q", str(root), f"refs/heads/{branch}:refs/heads/{branch}")
    from examples.mcp_server import control_plane_git as git

    result = git.git_fetch_ref_control_plane(
        receipt.project_id, remote="origin", branch="main", expected_remote_head=m.target
    )
    assert result["ok"], result
    assert run(root, "rev-parse", "HEAD") == head
    assert run(root, "rev-parse", "refs/remotes/origin/main") == m.target
    assert not (root / ".git/FETCH_HEAD").exists()


@pytest.mark.parametrize("ref", ["origin/main", "refs/remotes/origin/main"])
def test_tracking_alias_requires_remote_proof_even_in_local_only_source(
    registry_fixture, monkeypatch, ref
):
    workspace, source, config, journal, base = registry_fixture
    _git(source, "update-ref", "refs/remotes/origin/main", base)
    monkeypatch.setattr(
        clones,
        "_probe_remote_ref",
        lambda *_: clones.RemoteRefProbe(clones.RemoteRefStatus.UNKNOWN),
    )
    with pytest.raises(clones.CandidateCloneError, match="trusted remote state is unknown"):
        clones.prepare_candidate_clone(
            "source-project", "fix/no-remote-proof", ref, config_dir=config, journal_root=journal
        )
    assert not (workspace / ".mcp-candidate-clones").exists()


@pytest.mark.parametrize("prefix", [".registered-source-", ".candidate-remote-"])
def test_recorded_factory_origin_is_supported_without_rewriting_it(managed, prefix):
    m = managed
    origin = str(m.root.parent / (prefix + "fixture") / "source.bundle")
    run(m.root, "remote", "set-url", "origin", origin)
    m.metadata.update(clone_origin=origin, head=m.base, status_entries=0)
    m.metadata_path.write_text(json.dumps(m.metadata))
    before = (m.root / ".git/config").read_bytes()
    assert fetch(m)["ok"]
    assert (m.root / ".git/config").read_bytes() == before
    assert_preserved(m)


@pytest.mark.parametrize("mutation", ["missing", "mismatch", "foreign_path", "wrong_initial_head"])
def test_origin_proof_is_not_inferred_from_an_arbitrary_bundle(managed, mutation):
    m = managed
    origin = str(m.root.parent / ".registered-source-fixture/source.bundle")
    m.metadata.update(clone_origin=origin, head=m.base, status_entries=0)
    if mutation == "missing":
        m.metadata.pop("clone_origin")
    elif mutation == "mismatch":
        m.metadata["clone_origin"] += ".other"
    elif mutation == "foreign_path":
        origin = str(m.root.parent.parent / ".registered-source-fixture/source.bundle")
        m.metadata["clone_origin"] = origin
    else:
        m.metadata["head"] = m.head
    run(m.root, "remote", "set-url", "origin", origin)
    m.metadata_path.write_text(json.dumps(m.metadata))
    result = fetch(m)
    assert not result["ok"]
    assert result["error"]["details"]["stage"] == "candidate_identity"
    assert not m.published
    assert_preserved(m)
