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


@pytest.mark.parametrize("blocker", ["unwritable-directory", "foreign-owned-directory"])
def test_abandon_preflights_generated_assets_before_unregister(
    registry_fixture, monkeypatch, blocker
) -> None:
    """A root-created ignored build directory must never cause partial rmtree."""
    import os

    root, args = _empty_candidate(registry_fixture, monkeypatch)
    registry = args["config_dir"] / "projects.yaml"
    initial_registry = registry.read_bytes()
    excluded = root / ".git" / "info" / "exclude"
    excluded.write_text(
        excluded.read_text(encoding="utf-8") + "\n.build-data/\n",
        encoding="utf-8",
    )
    generated = root / ".build-data" / "audit"
    generated.mkdir(parents=True)
    marker = generated / "private-receipt"
    marker.write_text("preserve this generated artifact\n", encoding="utf-8")
    assert _git(root, "status", "--porcelain") == ""

    with monkeypatch.context() as restricted:
        if blocker == "unwritable-directory":
            original_access = os.access

            def deny_write(path, mode, **kwargs):
                if Path(path) == generated and mode & os.W_OK:
                    return False
                return original_access(path, mode, **kwargs)

            restricted.setattr(module.os, "access", deny_write)
        else:
            original_lstat = Path.lstat

            def fake_foreign_owner(path):
                result = original_lstat(path)
                if path == generated:
                    fields = list(result)
                    fields[4] = result.st_uid + 1
                    return os.stat_result(fields)
                return result

            restricted.setattr(Path, "lstat", fake_foreign_owner)

        with pytest.raises(CandidateCloneError) as excinfo:
            candidate_cleanup(**args)
        assert excinfo.value.code == "WORKSPACE_CONTENDED"
        assert registry.read_bytes() == initial_registry
        assert marker.read_text(encoding="utf-8") == "preserve this generated artifact\n"
        assert _git(root, "rev-parse", "HEAD") == args["expected_head_sha"]
        tombstone, _ = module._cleanup_tombstone_path(
            args["journal_root"], args["project_id"]
        )
        assert not tombstone.exists()

    # Once the foreign ownership/permission condition is resolved, the exact
    # unchanged candidate can still be retired through normal guarded cleanup.
    completed = candidate_cleanup(**args)
    assert completed.registry_removed and completed.directory_removed
    assert not root.exists()


def test_abandon_real_unwritable_generated_directory_keeps_registry(
    registry_fixture, monkeypatch
) -> None:
    """Exercise actual Linux filesystem permissions, not only a mocked probe."""
    import os

    root, args = _empty_candidate(registry_fixture, monkeypatch)
    registry = args["config_dir"] / "projects.yaml"
    registry_before = registry.read_bytes()
    excluded = root / ".git" / "info" / "exclude"
    excluded.write_text(
        excluded.read_text(encoding="utf-8") + "\n.build-data/\n",
        encoding="utf-8",
    )
    generated = root / ".build-data" / "audit"
    generated.mkdir(parents=True)
    marker = generated / "owned-output.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    assert _git(root, "status", "--porcelain") == ""

    generated.chmod(0o500)
    try:
        if os.access(generated, os.W_OK, effective_ids=True):
            pytest.skip("privileged test executor can write a 0500 directory")
        with pytest.raises(CandidateCloneError) as excinfo:
            candidate_cleanup(**args)
        assert excinfo.value.code == "WORKSPACE_CONTENDED"
        assert excinfo.value.details == {"failure_class": "directory_permission"}
        assert registry.read_bytes() == registry_before
        assert marker.read_text(encoding="utf-8") == "preserve\n"
        assert _git(root, "rev-parse", "HEAD") == args["expected_head_sha"]
    finally:
        generated.chmod(0o700)

    result = candidate_cleanup(**args)
    assert result.registry_removed and result.directory_removed
    assert not root.exists()


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
