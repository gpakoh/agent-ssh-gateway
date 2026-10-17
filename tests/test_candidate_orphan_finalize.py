"""Recovery tests for delivered candidate tails that lost registry/Git metadata."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.workspace.registry import reset_registry
from examples.mcp_server import candidate_orphan_finalize as module
from examples.mcp_server.candidate_clone import CandidateCloneError, prepare_candidate_clone
from examples.mcp_server.project_registry_control import register_project, unregister_project_exact
from tests.test_candidate_clone import registry_fixture as registry_fixture


def _idle_guard() -> None:
    return None


def _orphanize(registry_fixture, *, branch: str):
    workspace, _source, config_dir, journal_root, base = registry_fixture
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    root = workspace / ".mcp-candidate-clones" / receipt.project_id
    unregister_project_exact(
        config_dir=config_dir,
        journal_root=journal_root,
        project_id=receipt.project_id,
        expected_root=f".mcp-candidate-clones/{receipt.project_id}",
        expected_type="candidate-clone",
    )
    reset_registry()
    shutil.rmtree(root / ".git")
    return root, config_dir, journal_root, base, receipt.project_id


def _mock_delivery(monkeypatch: pytest.MonkeyPatch, *, entries: int = 1) -> None:
    monkeypatch.setattr(
        module,
        "_prove_delivered_subset",
        lambda **_kwargs: ("main", "f" * 40, entries),
    )


def test_orphan_finalize_removes_exact_unregistered_no_git_tail_and_is_idempotent(
    registry_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "candidate/orphan-success"
    root, config_dir, journal_root, base, project_id = _orphanize(
        registry_fixture, branch=branch
    )
    _mock_delivery(monkeypatch, entries=3)

    result = module.candidate_orphan_finalize(
        project_id,
        base,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    assert result.directory_removed is True
    assert result.already_finalized is False
    assert result.entries_verified == 3
    assert not root.exists()

    repeated = module.candidate_orphan_finalize(
        project_id,
        base,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert repeated.directory_removed is False
    assert repeated.already_finalized is True


def test_orphan_finalize_rejects_identity_not_bound_to_project_id(
    registry_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "candidate/orphan-identity"
    root, config_dir, journal_root, base, project_id = _orphanize(
        registry_fixture, branch=branch
    )
    _mock_delivery(monkeypatch)

    with pytest.raises(CandidateCloneError) as caught:
        module.candidate_orphan_finalize(
            project_id,
            "a" * 40,
            branch,
            "source-project",
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert caught.value.code == "POLICY_DENIED"
    assert root.is_dir()


def test_orphan_finalize_rejects_registry_reappearance(
    registry_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/orphan-registry"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    root = workspace / ".mcp-candidate-clones" / receipt.project_id
    shutil.rmtree(root / ".git")
    _mock_delivery(monkeypatch)

    with pytest.raises(CandidateCloneError) as caught:
        module.candidate_orphan_finalize(
            receipt.project_id,
            base,
            branch,
            "source-project",
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert caught.value.code == "WORKSPACE_CONTENDED"
    assert root.is_dir()


def test_orphan_finalize_rejects_registered_descendant_root(
    registry_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "candidate/orphan-descendant"
    root, config_dir, journal_root, base, project_id = _orphanize(
        registry_fixture, branch=branch
    )
    nested = root / "nested-project"
    nested.mkdir()
    register_project(
        config_dir=config_dir,
        journal_root=journal_root,
        project_id="nested-orphan-project",
        root=f".mcp-candidate-clones/{project_id}/nested-project",
        project_type="inspection",
        persist_to_source=True,
    )
    reset_registry()
    _mock_delivery(monkeypatch)

    with pytest.raises(CandidateCloneError) as caught:
        module.candidate_orphan_finalize(
            project_id,
            base,
            branch,
            "source-project",
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert caught.value.code == "WORKSPACE_CONTENDED"
    assert caught.value.details == {"registered_project_id": "nested-orphan-project"}
    assert root.is_dir()


def test_orphan_finalize_retries_same_leased_directory_after_rmtree_failure(
    registry_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "candidate/orphan-rmtree-retry"
    root, config_dir, journal_root, base, project_id = _orphanize(
        registry_fixture, branch=branch
    )
    _mock_delivery(monkeypatch, entries=2)
    real_rmtree = module.shutil.rmtree
    failed = False

    def fail_once(path: Path, *args, **kwargs) -> None:
        nonlocal failed
        if Path(path) == root and not failed:
            failed = True
            raise OSError("simulated interrupted orphan removal")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "rmtree", fail_once)
    with pytest.raises(CandidateCloneError, match="removal did not complete") as caught:
        module.candidate_orphan_finalize(
            project_id,
            base,
            branch,
            "source-project",
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert caught.value.retryable is True
    tombstone_path, _ = module._orphan_tombstone_path(journal_root, project_id)
    prepared = module._read_orphan_tombstone(tombstone_path)
    assert prepared is not None and prepared["phase"] == "prepared"
    assert root.is_dir()

    monkeypatch.setattr(module.shutil, "rmtree", real_rmtree)
    recovered = module.candidate_orphan_finalize(
        project_id,
        base,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert recovered.directory_removed is True
    assert not root.exists()
    completed = module._read_orphan_tombstone(tombstone_path)
    assert completed is not None and completed["phase"] == "complete"


def test_orphan_finalize_recovers_when_complete_tombstone_write_fails_after_delete(
    registry_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "candidate/orphan-complete-retry"
    root, config_dir, journal_root, base, project_id = _orphanize(
        registry_fixture, branch=branch
    )
    _mock_delivery(monkeypatch, entries=4)
    real_write = module._write_orphan_tombstone
    failed_complete = False

    def fail_first_complete(path: Path, data: dict[str, object]) -> None:
        nonlocal failed_complete
        if data.get("phase") == "complete" and not failed_complete:
            failed_complete = True
            raise CandidateCloneError(
                "TOOL_EXECUTION_FAILED",
                "simulated complete tombstone interruption",
                retryable=True,
            )
        real_write(path, data)

    monkeypatch.setattr(module, "_write_orphan_tombstone", fail_first_complete)
    with pytest.raises(CandidateCloneError, match="complete tombstone interruption"):
        module.candidate_orphan_finalize(
            project_id,
            base,
            branch,
            "source-project",
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert not root.exists()
    tombstone_path, _ = module._orphan_tombstone_path(journal_root, project_id)
    prepared = module._read_orphan_tombstone(tombstone_path)
    assert prepared is not None and prepared["phase"] == "prepared"

    monkeypatch.setattr(module, "_write_orphan_tombstone", real_write)
    reconciled = module.candidate_orphan_finalize(
        project_id,
        base,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert reconciled.already_finalized is True
    assert reconciled.directory_removed is False
    completed = module._read_orphan_tombstone(tombstone_path)
    assert completed is not None and completed["phase"] == "complete"


def test_delivered_subset_proof_requires_base_in_remote_default_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from examples.mcp_server.agent_sources import ManagedSourcePublication
    from examples.mcp_server.candidate_clone import RemoteDefaultHeadProbe, RemoteRefStatus
    from tests.test_candidate_clone import _commit, _git, _init_repo

    source = tmp_path / "source"
    base = _init_repo(source)
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    shutil.copy2(source / "README.md", orphan / "README.md")
    default_head = _commit(source, "new remote default")
    branch = _git(source, "branch", "--show-current")
    bundle = tmp_path / "default.bundle"
    _git(source, "bundle", "create", str(bundle), branch)

    monkeypatch.setattr(
        module,
        "_probe_remote_default_head",
        lambda _root: RemoteDefaultHeadProbe(
            RemoteRefStatus.FOUND,
            branch="main",
            sha=default_head,
        ),
    )
    monkeypatch.setattr(module, "_require_delivery_branch_absent", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        module,
        "ensure_managed_source_bundle",
        lambda _project, sha: ManagedSourcePublication(str(bundle), "0" * 64)
        if sha == default_head
        else None,
    )

    default_branch, proven_head, entries = module._prove_delivered_subset(
        source_project="source-project",
        source_root=source,
        candidate_root=orphan,
        base_sha=base,
        expected_branch="candidate/orphan-proof",
    )

    assert default_branch == "main"
    assert proven_head == default_head
    assert entries == 1


def test_subset_proof_accepts_only_entries_from_exact_git_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    from tests.test_candidate_clone import _git, _init_repo

    base = _init_repo(source)
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    shutil.copy2(source / "README.md", orphan / "README.md")

    assert module._verify_subset(orphan, source, base) == 1

    (orphan / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(CandidateCloneError, match="content differs"):
        module._verify_subset(orphan, source, base)

    (orphan / "README.md").write_text("base\n", encoding="utf-8")
    (orphan / "extra.txt").write_text("not delivered\n", encoding="utf-8")
    with pytest.raises(CandidateCloneError, match="not present"):
        module._verify_subset(orphan, source, base)

    assert _git(source, "status", "--porcelain=v1") == ""
