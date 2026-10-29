from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.workspace.registry import reset_registry
from examples.mcp_server import candidate_clone as module
from examples.mcp_server.candidate_clone import (
    CandidateCloneError,
    adopt_candidate_lineage,
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


def test_abort_legacy_prepared_cleanup_archives_only_journal(
    lineage_fixture,
) -> None:
    workspace, _source, config_dir, journal_root, head = lineage_fixture
    project_id = "legacy-git-source"
    branch = "fix/legacy-git-source"
    legacy_root = f".candidates/{project_id}"
    legacy = workspace / legacy_root
    legacy.mkdir(parents=True)
    marker = legacy / "unmodified.txt"
    marker.write_text("preserve me", encoding="utf-8")
    registry = config_dir / "projects.yaml"
    registry.write_text(
        registry.read_text(encoding="utf-8")
        + f"  {project_id}:\n"
        + f"    root: {legacy_root}\n"
        + "    type: git-source\n",
        encoding="utf-8",
    )
    before = registry.read_bytes()
    path, _ = module._cleanup_tombstone_path(journal_root, project_id)
    module._write_cleanup_tombstone(path, {
        "version": 1, "project_id": project_id, "source_project": "source-project",
        "branch": branch, "head": head, "preserved_ref": None,
        "registry_root": f".mcp-candidate-clones/{project_id}", "phase": "prepared",
    })
    kwargs = dict(
        project_id=project_id, expected_head_sha=head, expected_branch=branch,
        expected_source_project="source-project", expected_legacy_root=legacy_root,
        config_dir=config_dir, journal_root=journal_root,
    )
    preview = module.abort_legacy_prepared_cleanup(**kwargs)
    assert preview["outcome"] == "eligible"
    assert preview["journal_removed"] is False
    assert path.exists()

    with pytest.raises(CandidateCloneError) as no_lease:
        module.abort_legacy_prepared_cleanup(
            **kwargs, apply=True, reason="Operator approved exact journal recovery",
            reference_guard=lambda: None,
        )
    assert no_lease.value.code == "INVALID_INPUT"
    assert path.exists()

    with pytest.raises(CandidateCloneError) as stale:
        module.abort_legacy_prepared_cleanup(
            **kwargs, apply=True, expected_tombstone_sha256="0" * 64,
            reason="Operator approved exact journal recovery",
            reference_guard=lambda: None,
        )
    assert stale.value.code == "WORKSPACE_CONTENDED"
    assert path.exists()

    def audit_unavailable() -> None:
        raise CandidateCloneError("AUDIT_UNAVAILABLE", "audit is unavailable")

    with pytest.raises(CandidateCloneError) as no_audit:
        module.abort_legacy_prepared_cleanup(
            **kwargs, apply=True,
            expected_tombstone_sha256=preview["tombstone_sha256"],
            reason="Operator approved exact journal recovery",
            reference_guard=audit_unavailable,
        )
    assert no_audit.value.code == "AUDIT_UNAVAILABLE"
    assert path.exists()

    finished = module.abort_legacy_prepared_cleanup(
        **kwargs, apply=True,
        expected_tombstone_sha256=preview["tombstone_sha256"],
        reason="Operator approved exact journal recovery",
        reference_guard=lambda: None,
    )
    assert finished["outcome"] == "archived"
    assert finished["journal_removed"] is True
    assert not path.exists()
    assert len(list(path.parent.glob(f"{path.stem}.legacy-aborted-*.json"))) == 1
    assert registry.read_bytes() == before
    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_abort_legacy_prepared_cleanup_rejects_other_phase_and_managed_registry(
    lineage_fixture,
) -> None:
    workspace, _source, config_dir, journal_root, head = lineage_fixture
    project_id = "legacy-fail-closed"
    branch = "fix/legacy-fail-closed"
    legacy_root = f".candidates/{project_id}"
    (workspace / legacy_root).mkdir(parents=True)
    registry = config_dir / "projects.yaml"
    registry.write_text(
        registry.read_text(encoding="utf-8")
        + f"  {project_id}:\n"
        + f"    root: {legacy_root}\n"
        + "    type: git-source\n",
        encoding="utf-8",
    )
    path, _ = module._cleanup_tombstone_path(journal_root, project_id)
    record = {
        "version": 1, "project_id": project_id, "source_project": "source-project",
        "branch": branch, "head": head, "preserved_ref": None,
        "registry_root": f".mcp-candidate-clones/{project_id}",
        "phase": "registry_removed",
    }
    module._write_cleanup_tombstone(path, record)
    kwargs = dict(
        project_id=project_id, expected_head_sha=head, expected_branch=branch,
        expected_source_project="source-project", expected_legacy_root=legacy_root,
        config_dir=config_dir, journal_root=journal_root,
    )
    with pytest.raises(CandidateCloneError) as partial:
        module.abort_legacy_prepared_cleanup(**kwargs)
    assert partial.value.code == "WORKSPACE_CONTENDED"
    assert path.exists()

    record["phase"] = "prepared"
    module._write_cleanup_tombstone(path, record)
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            "type: git-source", "type: candidate-clone",
        ),
        encoding="utf-8",
    )
    with pytest.raises(CandidateCloneError) as wrong_owner:
        module.abort_legacy_prepared_cleanup(**kwargs)
    assert wrong_owner.value.code == "WORKSPACE_CONTENDED"
    assert path.exists()


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
