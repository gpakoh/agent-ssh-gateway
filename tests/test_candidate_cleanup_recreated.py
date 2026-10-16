"""Re-created candidates must not inherit an earlier incarnation's cleanup lease."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from app.workspace.registry import reset_registry
from examples.mcp_server import candidate_clone as module
from examples.mcp_server.candidate_clone import CandidateCloneError, candidate_cleanup
from tests.test_candidate_clone import (
    _commit,
    _idle_guard,
    _mock_cleanup_remote_probe,
    prepare_candidate_clone,
)
from tests.test_candidate_clone import registry_fixture as registry_fixture


def _recreated(registry_fixture, monkeypatch):
    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/recreated-cleanup"
    receipt = prepare_candidate_clone(
        "source-project", branch, base, config_dir=config_dir, journal_root=journal_root
    )
    root = workspace / ".mcp-candidate-clones" / receipt.project_id
    old_stat = root.stat()
    retired = workspace / "retired-incarnation"
    root.rename(retired)
    shutil.copytree(retired, root)
    head = _commit(root, "restored candidate")
    preserved = "archive/candidate-restored"
    tombstone = {
        "version": 1,
        "project_id": receipt.project_id,
        "source_project": "source-project",
        "branch": branch,
        "head": base,
        "preserved_ref": "archive/candidate-retired",
        "registry_root": f".mcp-candidate-clones/{receipt.project_id}",
        "phase": "complete",
        "root_dev": old_stat.st_dev,
        "root_ino": old_stat.st_ino,
    }
    path, _ = module._cleanup_tombstone_path(journal_root, receipt.project_id)
    module._write_cleanup_tombstone(path, tombstone)
    _mock_cleanup_remote_probe(
        monkeypatch, module, preserved_ref=preserved, head=head, delivery_branch=branch
    )
    args = (receipt.project_id, head, branch, "source-project", preserved)
    kwargs = dict(config_dir=config_dir, journal_root=journal_root, reference_guard=_idle_guard)
    return root, retired, path, tombstone, args, kwargs


def test_recreated_candidate_archives_completed_history_and_cleans_current_identity(
    registry_fixture, monkeypatch
):
    root, retired, path, previous, args, kwargs = _recreated(registry_fixture, monkeypatch)
    result = candidate_cleanup(*args, **kwargs)
    assert result.directory_removed and result.registry_removed
    assert not root.exists()
    assert retired.is_dir()
    current = json.loads(path.read_text())
    assert current["head"] == args[1]
    assert current["phase"] == "complete"
    history = path.with_name(current["previous_completed_tombstone"])
    assert json.loads(history.read_text()) == previous
    assert candidate_cleanup(*args, **kwargs).already_cleaned


@pytest.mark.parametrize(
    "blocker", ["dirty", "active", "dependency", "guard", "preservation", "registry", "identity"]
)
def test_recreated_candidate_still_enforces_current_guards_before_journal_mutation(
    registry_fixture, monkeypatch, blocker
):
    root, _retired, path, _previous, args, kwargs = _recreated(registry_fixture, monkeypatch)
    before = path.read_bytes()
    registry = kwargs["config_dir"] / "projects.yaml"
    registry_before = registry.read_bytes()
    if blocker == "dirty":
        (root / "uncommitted.txt").write_text("unfinished work")
    elif blocker == "active":
        (root / ".ai-bridge").mkdir()
        (root / ".ai-bridge" / "current-plan.md").write_text("still active")
    elif blocker == "dependency":
        monkeypatch.setattr(
            module, "_require_no_inbound_candidate_dependencies",
            lambda *_a: (_ for _ in ()).throw(CandidateCloneError("WORKSPACE_CONTENDED", "dependent")),
        )
    elif blocker == "guard":
        kwargs["reference_guard"] = lambda: (_ for _ in ()).throw(
            CandidateCloneError("WORKSPACE_CONTENDED", "open reference")
        )
    elif blocker == "preservation":
        monkeypatch.setattr(
            module, "_probe_remote_ref", lambda *_a: module.RemoteRefProbe(module.RemoteRefStatus.ABSENT)
        )
    elif blocker == "registry":
        data = yaml.safe_load(registry.read_text())
        data["projects"][args[0]]["type"] = "repository"
        registry.write_text(yaml.safe_dump(data, sort_keys=False))
        registry_before = registry.read_bytes()
        reset_registry()
    else:
        metadata_path = root / ".git" / "mcp-candidate-clone.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["source_project"] = "unrelated-source"
        metadata_path.write_text(json.dumps(metadata))

    with pytest.raises(CandidateCloneError):
        candidate_cleanup(*args, **kwargs)
    assert root.is_dir()
    assert path.read_bytes() == before
    assert registry.read_bytes() == registry_before
    assert not list(path.parent.glob(f"{path.stem}.completed-*.json"))


@pytest.mark.parametrize("phase", ["prepared", "registry_removed", "unknown"])
def test_recreated_candidate_never_replaces_an_unfinished_or_invalid_transaction(
    registry_fixture, monkeypatch, phase
):
    root, _retired, path, previous, args, kwargs = _recreated(registry_fixture, monkeypatch)
    previous["phase"] = phase
    module._write_cleanup_tombstone(path, previous)
    before = path.read_bytes()
    with pytest.raises(CandidateCloneError):
        candidate_cleanup(*args, **kwargs)
    assert root.is_dir() and path.read_bytes() == before


@pytest.mark.parametrize("field", ["project_id", "source_project", "branch", "registry_root"])
def test_recreated_candidate_refuses_foreign_completed_history(
    registry_fixture, monkeypatch, field
):
    root, _retired, path, previous, args, kwargs = _recreated(registry_fixture, monkeypatch)
    previous[field] = "foreign"
    module._write_cleanup_tombstone(path, previous)
    before = path.read_bytes()
    with pytest.raises(CandidateCloneError):
        candidate_cleanup(*args, **kwargs)
    assert root.is_dir() and path.read_bytes() == before


def test_recreated_candidate_rejects_same_directory_with_different_head(
    registry_fixture, monkeypatch
):
    root, _retired, path, previous, args, kwargs = _recreated(registry_fixture, monkeypatch)
    previous.update(root_dev=root.stat().st_dev, root_ino=root.stat().st_ino)
    module._write_cleanup_tombstone(path, previous)
    before = path.read_bytes()
    with pytest.raises(CandidateCloneError):
        candidate_cleanup(*args, **kwargs)
    assert root.is_dir() and path.read_bytes() == before


def test_recreated_candidate_resumes_after_registry_removal(
    registry_fixture, monkeypatch
):
    root, _retired, path, previous, args, kwargs = _recreated(registry_fixture, monkeypatch)
    real_remove = module.shutil.rmtree
    def fail_remove(target, *a, **kw):
        if Path(target) == root:
            raise OSError("interrupted cleanup")
        return real_remove(target, *a, **kw)
    monkeypatch.setattr(module.shutil, "rmtree", fail_remove)
    with pytest.raises(CandidateCloneError, match="removal did not complete"):
        candidate_cleanup(*args, **kwargs)
    interrupted = json.loads(path.read_text())
    assert interrupted["phase"] == "registry_removed"
    assert json.loads(path.with_name(interrupted["previous_completed_tombstone"]).read_text()) == previous
    monkeypatch.setattr(module.shutil, "rmtree", real_remove)
    result = candidate_cleanup(*args, **kwargs)
    assert result.directory_removed and not result.registry_removed
    assert not root.exists()


def test_recreated_candidate_resumes_after_rmtree_removed_git_metadata(
    registry_fixture, monkeypatch
):
    root, _retired, path, _previous, args, kwargs = _recreated(registry_fixture, monkeypatch)
    real_remove = module.shutil.rmtree

    def partially_remove(target, *a, **kw):
        if Path(target) == root:
            real_remove(root / ".git")
            raise OSError("interrupted after git metadata removal")
        return real_remove(target, *a, **kw)

    monkeypatch.setattr(module.shutil, "rmtree", partially_remove)
    with pytest.raises(CandidateCloneError, match="removal did not complete"):
        candidate_cleanup(*args, **kwargs)

    interrupted = json.loads(path.read_text())
    assert interrupted["phase"] == "registry_removed"
    assert root.is_dir() and not (root / ".git").exists()
    assert not module._registry_has_candidate(kwargs["config_dir"], args[0])

    monkeypatch.setattr(module.shutil, "rmtree", real_remove)
    result = candidate_cleanup(*args, **kwargs)
    assert result.directory_removed and not result.registry_removed
    assert not root.exists()
    assert json.loads(path.read_text())["phase"] == "complete"


def test_recreated_candidate_retry_after_history_saved_before_new_lease(
    registry_fixture, monkeypatch
):
    root, _retired, path, previous, args, kwargs = _recreated(registry_fixture, monkeypatch)
    original = module._write_cleanup_tombstone
    failed = False

    def interrupt(target, data):
        nonlocal failed
        if target == path and data.get("phase") == "prepared" and not failed:
            failed = True
            raise CandidateCloneError("TOOL_EXECUTION_FAILED", "interrupted before new lease")
        return original(target, data)

    monkeypatch.setattr(module, "_write_cleanup_tombstone", interrupt)
    with pytest.raises(CandidateCloneError, match="interrupted before new lease"):
        candidate_cleanup(*args, **kwargs)
    assert root.is_dir()
    assert json.loads(path.read_text()) == previous
    history = list(path.parent.glob(f"{path.stem}.completed-*.json"))
    assert len(history) == 1
    assert json.loads(history[0].read_text()) == previous
    assert candidate_cleanup(*args, **kwargs).directory_removed
    assert len(list(path.parent.glob(f"{path.stem}.completed-*.json"))) == 1


@pytest.mark.parametrize("unsafe", ["symlink", "conflicting_record"])
def test_recreated_candidate_history_must_be_safe_before_unregister(
    registry_fixture, monkeypatch, unsafe
):
    root, _retired, path, previous, args, kwargs = _recreated(registry_fixture, monkeypatch)
    digest = hashlib.sha256(json.dumps(previous, sort_keys=True).encode("utf-8")).hexdigest()
    history = path.with_name(f"{path.stem}.completed-{digest}.json")
    before = path.read_bytes()
    registry = kwargs["config_dir"] / "projects.yaml"
    registry_before = registry.read_bytes()
    if unsafe == "symlink":
        history.symlink_to(path)
    else:
        module._write_cleanup_tombstone(history, {"phase": "unrelated"})
    with pytest.raises(CandidateCloneError):
        candidate_cleanup(*args, **kwargs)
    assert root.is_dir()
    assert path.read_bytes() == before
    assert registry.read_bytes() == registry_before
