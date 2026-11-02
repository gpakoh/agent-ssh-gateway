"""Recovery tests for delivered candidate tails that lost registry/Git metadata."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

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


def test_orphan_subset_accepts_only_empty_site_audit_staging_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lost-Git Site Audit tails may contain four generated EMPTY dirs."""
    root = tmp_path / "orphan"
    (root / ".build-data/audit/artifact-staging").mkdir(parents=True)
    (root / ".build-data/audit/artifacts").mkdir()
    monkeypatch.setattr(module, "_tree_manifest", lambda *_: {})

    with pytest.raises(CandidateCloneError, match="content not present"):
        module._verify_subset(root, tmp_path, "a" * 40)

    assert (
        module._verify_subset(
            root, tmp_path, "a" * 40, allow_empty_site_audit_staging=True
        )
        == 4
    )


@pytest.mark.parametrize(
    ("extra", "as_symlink"),
    [
        (".build-data/audit/artifacts/proof.txt", False),
        (".build-data/audit/artifact-staging/hidden-file", False),
        (".build-data/audit/other", False),
        (".build-data/audit/artifacts/link", True),
    ],
)
def test_orphan_subset_never_discards_untracked_bytes_or_unexpected_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str, as_symlink: bool
) -> None:
    root = tmp_path / "orphan"
    (root / ".build-data/audit/artifact-staging").mkdir(parents=True)
    (root / ".build-data/audit/artifacts").mkdir()
    target = root / extra
    if as_symlink:
        target.symlink_to(tmp_path / "elsewhere")
    elif "." in target.name or target.name == "hidden-file":
        target.write_bytes(b"preserve original bytes")
    else:
        target.mkdir()
    monkeypatch.setattr(module, "_tree_manifest", lambda *_: {})

    with pytest.raises(CandidateCloneError, match="content not present"):
        module._verify_subset(
            root, tmp_path, "a" * 40, allow_empty_site_audit_staging=True
        )
    assert target.is_symlink() if as_symlink else target.exists()


def test_orphan_subset_rejects_symlink_instead_of_generated_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "orphan"
    (root / ".build-data/audit").mkdir(parents=True)
    (root / ".build-data/audit/artifacts").symlink_to(tmp_path / "other-dir")
    monkeypatch.setattr(module, "_tree_manifest", lambda *_: {})

    with pytest.raises(CandidateCloneError):
        module._verify_subset(
            root, tmp_path, "a" * 40, allow_empty_site_audit_staging=True
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


def _add_orphan_guard_registry_project(
    config_dir: Path, *, project_id: str, entry: dict[str, object]
) -> None:
    registry_path = config_dir / "projects.yaml"
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    registry["projects"][project_id] = entry
    registry_path.write_text(
        yaml.safe_dump(registry, sort_keys=False), encoding="utf-8"
    )
    reset_registry()


def test_orphan_finalize_ignores_valid_rootless_coordination_only_project(
    registry_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "candidate/orphan-coordination-only"
    root, config_dir, journal_root, base, project_id = _orphanize(
        registry_fixture, branch=branch
    )
    _add_orphan_guard_registry_project(
        config_dir,
        project_id="nod-shared",
        entry={
            "type": "coordination-only",
            "gitea_repo": "gpakoh/nod-shared",
            "description": "No filesystem root by design",
        },
    )
    _mock_delivery(monkeypatch)

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
    assert not root.exists()


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "python-service", "gitea_repo": "gpakoh/nod-shared"},
        {"type": "coordination-only", "gitea_repo": "gpakoh/nod-shared", "root": ""},
        {"type": "coordination-only", "gitea_repo": "gpakoh/nod-shared", "root_selector": "default"},
        {"type": "coordination-only", "gitea_repo": "../outside"},
        {"type": "coordination-only"},
    ],
)
def test_orphan_finalize_rejects_other_unresolved_project_roots(
    registry_fixture, monkeypatch: pytest.MonkeyPatch, entry: dict[str, object]
) -> None:
    branch = "candidate/orphan-invalid-rootless"
    root, config_dir, journal_root, base, project_id = _orphanize(
        registry_fixture, branch=branch
    )
    _add_orphan_guard_registry_project(
        config_dir, project_id="ambiguous-project", entry=entry
    )
    _mock_delivery(monkeypatch)

    with pytest.raises(CandidateCloneError, match="unresolved project root") as caught:
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
    assert root.exists()


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


def test_orphan_finalize_denies_root_owned_generated_directory_before_journal(
    registry_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "candidate/orphan-root-owned"
    root, config_dir, journal_root, base, project_id = _orphanize(
        registry_fixture, branch=branch
    )
    (root / ".build-data/audit/artifact-staging").mkdir(parents=True)
    (root / ".build-data/audit/artifacts").mkdir()
    _mock_delivery(monkeypatch, entries=4)
    real_access = module.os.access
    blocked = root / ".build-data"

    def deny_root_owned_staging(path, mode, *, effective_ids=False):
        if Path(path) == blocked:
            return False
        return real_access(path, mode, effective_ids=effective_ids)

    monkeypatch.setattr(module.os, "access", deny_root_owned_staging)
    with pytest.raises(CandidateCloneError, match="host operator filesystem permission") as caught:
        module.candidate_orphan_finalize(
            project_id,
            base,
            branch,
            "source-project",
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.retryable is False
    assert caught.value.details == {
        "path": ".build-data",
        "recovery_action": "host_operator_required",
    }
    assert (root / ".build-data/audit/artifacts").is_dir()
    tombstone, _ = module._orphan_tombstone_path(journal_root, project_id)
    assert module._read_orphan_tombstone(tombstone) is None

    # With permission restored, the same unchanged original candidate is
    # eligible for the normal exact-provenance path, not a blind retry.
    monkeypatch.setattr(module.os, "access", real_access)
    receipt = module.candidate_orphan_finalize(
        project_id,
        base,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert receipt.directory_removed is True
    assert not root.exists()


def test_orphan_finalize_permission_error_during_remove_is_nonretryable(
    registry_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "candidate/orphan-rmtree-eacces"
    root, config_dir, journal_root, base, project_id = _orphanize(
        registry_fixture, branch=branch
    )
    _mock_delivery(monkeypatch)
    real_rmtree = module.shutil.rmtree

    def deny_removal(path: Path, *args, **kwargs) -> None:
        if Path(path) == root:
            raise PermissionError(13, "Permission denied", str(root))
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "rmtree", deny_removal)
    with pytest.raises(CandidateCloneError, match="host operator filesystem permission") as caught:
        module.candidate_orphan_finalize(
            project_id,
            base,
            branch,
            "source-project",
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.retryable is False
    assert caught.value.details == {
        "path": ".",
        "recovery_action": "host_operator_required",
    }
    assert str(root) not in str(caught.value)
    journal, _ = module._orphan_tombstone_path(journal_root, project_id)
    assert module._read_orphan_tombstone(journal)["phase"] == "prepared"
    assert root.exists()


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

    (orphan / ".build-data/audit/artifact-staging").mkdir(parents=True)
    (orphan / ".build-data/audit/artifacts").mkdir()
    with pytest.raises(CandidateCloneError, match="content not present"):
        module._prove_delivered_subset(
            source_project="source-project",
            source_root=source,
            candidate_root=orphan,
            base_sha=base,
            expected_branch="candidate/orphan-proof",
        )
    _branch, _head, site_audit_entries = module._prove_delivered_subset(
        source_project="site-audit-platform",
        source_root=source,
        candidate_root=orphan,
        base_sha=base,
        expected_branch="candidate/orphan-proof",
    )
    assert site_audit_entries == 5


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
