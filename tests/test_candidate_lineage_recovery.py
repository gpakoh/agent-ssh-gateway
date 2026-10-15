from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.workspace.registry import reset_registry
from examples.mcp_server import candidate_clone as module
from examples.mcp_server.candidate_clone import (
    CandidateCloneError,
    ProtectedBranchEvidence,
    adopt_candidate_lineage,
    candidate_cleanup,
    prepare_candidate_clone,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _init_repo(root: Path) -> str:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "base")
    return _git(root, "rev-parse", "HEAD")


def _commit(repo: Path, message: str) -> str:
    (repo / "README.md").write_text(message + "\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def lineage_fixture(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source"
    base = _init_repo(source)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "projects.yaml").write_text(
        "version: 1\n"
        f"registry_root: {workspace}\n\n"
        "projects:\n"
        "  source-project:\n"
        "    root: source\n"
        "    type: repository\n"
        "    description: source project\n"
        "    tags: [source]\n",
        encoding="utf-8",
    )
    journal_root = tmp_path / "journals"
    reset_registry()
    try:
        yield workspace, source, config_dir, journal_root, base
    finally:
        reset_registry()


def _legacy_clone(workspace: Path, source: Path, project_id: str, branch: str) -> Path:
    candidates = workspace / ".mcp-candidate-clones"
    candidates.mkdir(parents=True, exist_ok=True)
    candidate = candidates / project_id
    _git(candidates, "clone", "-q", str(source), str(candidate))
    _git(candidate, "config", "user.name", "Test")
    _git(candidate, "config", "user.email", "test@example.invalid")
    _git(candidate, "branch", "-M", branch)
    return candidate


def _same_repo_identity(monkeypatch: pytest.MonkeyPatch, source: Path, candidate: Path) -> None:
    trusted = "https://trusted.invalid/gpakoh/agent-ssh-gateway.git"

    def resolve(root: Path) -> tuple[str, str, str]:
        resolved = root.resolve()
        assert resolved in {source.resolve(), candidate.resolve()}
        return "fixture-user", trusted, "unused-test-token"

    monkeypatch.setattr(module, "_resolve_trusted_remote", resolve)


def _delivery_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        module,
        "_probe_remote_ref",
        lambda _source, _ref: module.RemoteRefProbe(module.RemoteRefStatus.ABSENT),
    )


def _idle_guard() -> None:
    return None


def test_adopt_candidate_lineage_records_current_facts_without_inventing_history(
    lineage_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source, config_dir, journal_root, base = lineage_fixture
    project_id = "candidate-legacy-adopt"
    branch = "fix/legacy-adopt"
    candidate = _legacy_clone(workspace, source, project_id, branch)
    _same_repo_identity(monkeypatch, source, candidate)
    registry_before = (config_dir / "projects.yaml").read_bytes()

    adopted = adopt_candidate_lineage(
        project_id,
        base,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    assert adopted.already_adopted is False
    assert adopted.lineage_reconstructed is True
    assert _git(candidate, "rev-parse", "HEAD") == base
    assert _git(candidate, "rev-parse", "--abbrev-ref", "HEAD") == branch
    metadata = json.loads((candidate / ".git" / "mcp-candidate-clone.json").read_text())
    assert metadata == {
        "version": 2,
        "project_id": project_id,
        "source_project": "source-project",
        "branch": branch,
        "head": base,
        "root": f".mcp-candidate-clones/{project_id}",
        "lineage_reconstructed": True,
        "historical_provenance": {"kind": "unknown", "base_ref": None, "base_sha": None},
    }
    assert (config_dir / "projects.yaml").read_bytes() == registry_before

    repeated = adopt_candidate_lineage(
        project_id,
        base,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert repeated.already_adopted is True
    assert (config_dir / "projects.yaml").read_bytes() == registry_before


def test_adopt_candidate_lineage_rejects_wrong_head_and_dirty_candidate(
    lineage_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source, config_dir, journal_root, base = lineage_fixture
    project_id = "candidate-legacy-adopt-guard"
    branch = "fix/legacy-adopt-guard"
    candidate = _legacy_clone(workspace, source, project_id, branch)
    _same_repo_identity(monkeypatch, source, candidate)

    with pytest.raises(CandidateCloneError) as wrong_head:
        adopt_candidate_lineage(
            project_id,
            "f" * 40,
            branch,
            "source-project",
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert wrong_head.value.code == "WORKSPACE_CONTENDED"
    assert not (candidate / ".git" / "mcp-candidate-clone.json").exists()

    (candidate / "dirty.txt").write_text("keep\n", encoding="utf-8")
    with pytest.raises(CandidateCloneError) as dirty:
        adopt_candidate_lineage(
            project_id,
            base,
            branch,
            "source-project",
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert dirty.value.code == "WORKSPACE_CONTENDED"
    assert not (candidate / ".git" / "mcp-candidate-clone.json").exists()


def test_adopt_candidate_lineage_refuses_existing_cleanup_transaction(
    lineage_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source, config_dir, journal_root, base = lineage_fixture
    project_id = "candidate-legacy-adopt-cleanup-race"
    branch = "fix/legacy-adopt-cleanup-race"
    candidate = _legacy_clone(workspace, source, project_id, branch)
    _same_repo_identity(monkeypatch, source, candidate)
    tombstone_path, _ = module._cleanup_tombstone_path(journal_root, project_id)
    module._write_cleanup_tombstone(
        tombstone_path,
        {"version": 2, "phase": "prepared", "project_id": project_id},
    )

    with pytest.raises(CandidateCloneError) as caught:
        adopt_candidate_lineage(
            project_id,
            base,
            branch,
            "source-project",
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert caught.value.code == "WORKSPACE_CONTENDED"
    assert caught.value.details == {
        "project_id": project_id,
        "recovery_action": "resume_candidate_cleanup",
    }
    assert not (candidate / ".git" / "mcp-candidate-clone.json").exists()


def test_cleanup_rejects_unmerged_legacy_head_even_when_branch_is_protected(
    lineage_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source, config_dir, journal_root, base = lineage_fixture
    project_id = "candidate-legacy-unmerged"
    branch = "fix/legacy-unmerged"
    candidate = _legacy_clone(workspace, source, project_id, branch)
    feature_head = _commit(candidate, "feature-only")
    protected_tip = _commit(source, "protected-tip")
    _git(candidate, "fetch", "-q", "origin")
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", feature_head, protected_tip],
        cwd=candidate,
        check=False,
    ).returncode != 0
    _same_repo_identity(monkeypatch, source, candidate)
    _delivery_absent(monkeypatch)
    adopt_candidate_lineage(
        project_id,
        feature_head,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    evidence = ProtectedBranchEvidence("main", True, protected_tip)
    with pytest.raises(CandidateCloneError) as caught:
        candidate_cleanup(
            project_id,
            feature_head,
            branch,
            "source-project",
            protected_branch_evidence=evidence,
            protected_branch_refresher=lambda _branch: evidence,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert caught.value.code == "CHECK_FAILED"
    assert candidate.is_dir()
    assert _git(candidate, "rev-parse", "HEAD") == feature_head


def test_cleanup_removes_adopted_legacy_candidate_reachable_from_real_protected_tip(
    lineage_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source, config_dir, journal_root, base = lineage_fixture
    project_id = "candidate-legacy-reachable"
    branch = "fix/legacy-reachable"
    candidate = _legacy_clone(workspace, source, project_id, branch)
    protected_tip = _commit(source, "protected-tip")
    _git(candidate, "fetch", "-q", "origin")
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, protected_tip],
        cwd=candidate,
        check=False,
    ).returncode == 0
    _same_repo_identity(monkeypatch, source, candidate)
    _delivery_absent(monkeypatch)
    adopt_candidate_lineage(
        project_id,
        base,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    evidence = ProtectedBranchEvidence("main", True, protected_tip)

    cleaned = candidate_cleanup(
        project_id,
        base,
        branch,
        "source-project",
        protected_branch_evidence=evidence,
        protected_branch_refresher=lambda _branch: evidence,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    assert cleaned.preserved_ref is None
    assert cleaned.registry_removed is False
    assert cleaned.directory_removed is True
    assert not candidate.exists()


def test_cleanup_fails_closed_if_protected_branch_moves_before_rmtree(
    lineage_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source, config_dir, journal_root, base = lineage_fixture
    project_id = "candidate-legacy-race"
    branch = "fix/legacy-race"
    candidate = _legacy_clone(workspace, source, project_id, branch)
    protected_tip = _commit(source, "protected-tip")
    moved_tip = _commit(source, "protected-moved")
    _git(candidate, "fetch", "-q", "origin")
    _same_repo_identity(monkeypatch, source, candidate)
    _delivery_absent(monkeypatch)
    adopt_candidate_lineage(
        project_id,
        base,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    evidence = ProtectedBranchEvidence("main", True, protected_tip)
    rmtree_calls: list[Path] = []
    original_rmtree = module.shutil.rmtree

    def tracked_rmtree(path: Path) -> None:
        rmtree_calls.append(path)
        original_rmtree(path)

    monkeypatch.setattr(module.shutil, "rmtree", tracked_rmtree)

    with pytest.raises(CandidateCloneError) as caught:
        candidate_cleanup(
            project_id,
            base,
            branch,
            "source-project",
            protected_branch_evidence=evidence,
            protected_branch_refresher=lambda _branch: ProtectedBranchEvidence("main", True, moved_tip),
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert caught.value.code == "CHECK_FAILED"
    assert candidate.is_dir()
    assert rmtree_calls == []


def test_unprotected_branch_name_is_never_preservation_evidence(
    lineage_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source, config_dir, journal_root, base = lineage_fixture
    project_id = "candidate-legacy-unprotected"
    branch = "fix/legacy-unprotected"
    candidate = _legacy_clone(workspace, source, project_id, branch)
    _same_repo_identity(monkeypatch, source, candidate)
    _delivery_absent(monkeypatch)
    adopt_candidate_lineage(
        project_id,
        base,
        branch,
        "source-project",
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    with pytest.raises(CandidateCloneError) as caught:
        candidate_cleanup(
            project_id,
            base,
            branch,
            "source-project",
            protected_branch_evidence=ProtectedBranchEvidence("main", False, base),
            protected_branch_refresher=lambda _branch: ProtectedBranchEvidence("main", False, base),
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert caught.value.code == "CHECK_FAILED"
    assert candidate.is_dir()


@pytest.mark.parametrize("phase", ["prepared", "registry_removed", "complete"])
def test_v1_cleanup_tombstones_resume_under_v2_code(
    lineage_fixture,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    workspace, _source, config_dir, journal_root, base = lineage_fixture
    branch = f"fix/v1-resume-{phase}"
    preserved_ref = f"archive/candidate-v1-resume-{phase}"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    candidate = workspace / ".mcp-candidate-clones" / receipt.project_id

    def probe(_source: Path, ref: str):
        if ref == preserved_ref:
            return module.RemoteRefProbe(module.RemoteRefStatus.FOUND, base)
        if ref == branch:
            return module.RemoteRefProbe(module.RemoteRefStatus.ABSENT)
        return module.RemoteRefProbe(module.RemoteRefStatus.ABSENT)

    monkeypatch.setattr(module, "_probe_remote_ref", probe)
    tombstone_path, _ = module._cleanup_tombstone_path(journal_root, receipt.project_id)
    tombstone = {
        "version": 1,
        "project_id": receipt.project_id,
        "source_project": "source-project",
        "branch": branch,
        "head": base,
        "preserved_ref": preserved_ref,
        "registry_root": f".mcp-candidate-clones/{receipt.project_id}",
        "phase": phase,
    }
    if phase != "complete":
        root_stat = candidate.stat()
        tombstone["root_dev"] = root_stat.st_dev
        tombstone["root_ino"] = root_stat.st_ino
    if phase in {"registry_removed", "complete"}:
        module._unregister_candidate(
            config_dir=config_dir,
            journal_root=journal_root,
            project_id=receipt.project_id,
            root=f".mcp-candidate-clones/{receipt.project_id}",
        )
        reset_registry()
    if phase == "complete":
        shutil.rmtree(candidate)
    module._write_cleanup_tombstone(tombstone_path, tombstone)

    cleaned = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    assert not candidate.exists()
    assert module._read_cleanup_tombstone(tombstone_path)["phase"] == "complete"
    if phase == "complete":
        assert cleaned.already_cleaned is True
