"""Safe explicit cancellation of a never-modified managed candidate clone."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server import candidate_clone as module
from examples.mcp_server.candidate_clone import CandidateCloneError, candidate_cleanup
from tests.test_candidate_clone import _git, _idle_guard, prepare_candidate_clone
from tests.test_candidate_clone import registry_fixture as registry_fixture


def _empty_candidate(registry_fixture, monkeypatch):
    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "fix/abandon-unmodified"
    receipt = prepare_candidate_clone(
        "source-project", branch, base, config_dir=config_dir, journal_root=journal_root
    )
    root = workspace / ".mcp-candidate-clones" / receipt.project_id

    def remote_probe(_root: Path, ref: str):
        assert ref == branch
        return module.RemoteRefProbe(module.RemoteRefStatus.ABSENT)

    monkeypatch.setattr(module, "_probe_remote_ref", remote_probe)
    params = {
        "project_id": receipt.project_id,
        "expected_head_sha": base,
        "expected_branch": branch,
        "expected_source_project": "source-project",
        "preserved_ref": None,
        "abandon_unmodified": True,
        "config_dir": config_dir,
        "journal_root": journal_root,
        "reference_guard": _idle_guard,
    }
    return root, params


def test_abandon_exact_unmodified_candidate_is_guarded_idempotent(registry_fixture, monkeypatch):
    root, args = _empty_candidate(registry_fixture, monkeypatch)
    assert _git(root, "status", "--porcelain") == ""
    completed = candidate_cleanup(**args)
    assert completed.directory_removed and completed.registry_removed
    assert not root.exists()
    assert args["project_id"] not in args["config_dir"].joinpath("projects.yaml").read_text()
    assert candidate_cleanup(**args).already_cleaned


@pytest.mark.parametrize("mutation", ["dirty", "active", "other_ref", "unreachable", "provenance", "committed"])
def test_abandon_refuses_unique_or_active_candidate_without_mutation(registry_fixture, monkeypatch, mutation):
    root, args = _empty_candidate(registry_fixture, monkeypatch)
    if mutation == "dirty":
        (root / "unfinished.txt").write_text("keep", encoding="utf-8")
    elif mutation == "active":
        bridge = root / ".ai-bridge"
        bridge.mkdir()
        (bridge / "current-plan.md").write_text("active", encoding="utf-8")
    elif mutation == "other_ref":
        _git(root, "branch", "local-backup")
    elif mutation == "unreachable":
        subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=root, input="valuable unreferenced work", text=True, check=True, capture_output=True)
    elif mutation == "provenance":
        path = root / ".git" / "mcp-candidate-clone.json"
        meta = json.loads(path.read_text())
        meta["base_sha"] = "0" * 40
        path.write_text(json.dumps(meta), encoding="utf-8")
    else:
        (root / "README.md").write_text("modified", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "-m", "local unique commit")
    before = args["config_dir"].joinpath("projects.yaml").read_bytes()
    with pytest.raises(CandidateCloneError):
        candidate_cleanup(**args)
    assert root.exists()
    assert args["config_dir"].joinpath("projects.yaml").read_bytes() == before


@pytest.mark.parametrize("remote_state", ["published", "unknown"])
def test_abandon_refuses_remote_branch_or_unknown_remote(registry_fixture, monkeypatch, remote_state):
    root, args = _empty_candidate(registry_fixture, monkeypatch)
    result = module.RemoteRefStatus.FOUND if remote_state == "published" else module.RemoteRefStatus.UNKNOWN
    monkeypatch.setattr(
        module, "_probe_remote_ref",
        lambda *_a: module.RemoteRefProbe(result, args["expected_head_sha"] if remote_state == "published" else None),
    )
    with pytest.raises(CandidateCloneError):
        candidate_cleanup(**args)
    assert root.exists()


def test_abandon_is_explicit_and_cannot_bypass_reference_guard(registry_fixture, monkeypatch):
    root, args = _empty_candidate(registry_fixture, monkeypatch)
    not_opted = dict(args)
    not_opted.pop("abandon_unmodified")
    with pytest.raises(CandidateCloneError, match="delivery_guard"):
        candidate_cleanup(**not_opted)
    bad = dict(args, reference_guard=lambda: (_ for _ in ()).throw(
        CandidateCloneError("WORKSPACE_CONTENDED", "active external reference")
    ))
    with pytest.raises(CandidateCloneError, match="active external reference"):
        candidate_cleanup(**bad)
    assert root.exists()
    assert args["project_id"] in args["config_dir"].joinpath("projects.yaml").read_text()
