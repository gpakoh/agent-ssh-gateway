"""Evidence-preserving, owner-only discard of one untracked managed-candidate file."""

from __future__ import annotations

import hashlib
import os

import pytest

from examples.mcp_server.candidate_clone import CandidateCloneError
from examples.mcp_server.candidate_file_discard import discard_untracked_candidate_file
from tests.test_candidate_abandon_unmodified import _empty_candidate
from tests.test_candidate_clone import registry_fixture as registry_fixture


def _setup(registry_fixture, monkeypatch, *, content: bytes = b"abandoned scratch\n"):
    root, cleanup_args = _empty_candidate(registry_fixture, monkeypatch)
    path = root / "scratch.txt"
    path.write_bytes(content)
    params = {
        "project_id": cleanup_args["project_id"],
        "expected_head_sha": cleanup_args["expected_head_sha"],
        "expected_branch": cleanup_args["expected_branch"],
        "expected_source_project": cleanup_args["expected_source_project"],
        "relative_path": path.name,
        "expected_size": len(content),
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "config_dir": cleanup_args["config_dir"],
        "journal_root": cleanup_args["journal_root"],
        "reference_guard": lambda: None,
    }
    return root, path, params


def test_discard_single_exact_untracked_file_preserves_durable_archive(registry_fixture, monkeypatch):
    root, path, params = _setup(registry_fixture, monkeypatch)
    before = (root / ".git" / "mcp-candidate-clone.json").read_bytes()
    receipt = discard_untracked_candidate_file(**params)

    assert receipt["discarded"] is True
    assert receipt["archived_size"] == params["expected_size"]
    assert receipt["archived_sha256"] == params["expected_sha256"]
    assert not path.exists()
    assert (root / ".git" / "mcp-candidate-clone.json").read_bytes() == before
    archives = list((params["journal_root"] / "candidate-file-archive").glob("*.blob"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == b"abandoned scratch\n"
    assert archives[0].stat().st_mode & 0o077 == 0
    from tests.test_candidate_clone import _git

    assert _git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("mutation", ["sha", "size", "another", "tracked", "active", "hardlink", "symlink", "wrong-branch"])
def test_discard_rejects_all_nontrivial_candidate_mutations(registry_fixture, monkeypatch, mutation, tmp_path):
    root, path, params = _setup(registry_fixture, monkeypatch)
    if mutation == "sha":
        params["expected_sha256"] = "0" * 64
    elif mutation == "size":
        params["expected_size"] += 1
    elif mutation == "another":
        (root / "another-file.txt").write_text("important", encoding="utf-8")
    elif mutation == "tracked":
        (root / "README.md").write_text("important", encoding="utf-8")
    elif mutation == "active":
        bridge = root / ".ai-bridge"
        bridge.mkdir()
        (bridge / "current-plan.md").write_text("important", encoding="utf-8")
    elif mutation == "hardlink":
        os.link(path, tmp_path / "valuable-copy")
    elif mutation == "symlink":
        outside = tmp_path / "outside-secret"
        outside.write_text("important", encoding="utf-8")
        path.unlink()
        path.symlink_to(outside)
    else:
        params["expected_branch"] = "fix/unrelated"
    with pytest.raises(CandidateCloneError):
        discard_untracked_candidate_file(**params)
    assert path.exists() or path.is_symlink()
    assert not list((params["journal_root"] / "candidate-file-archive").glob("*.blob"))


@pytest.mark.parametrize("relative_path", ["/tmp/file", "../scratch.txt", ".git/config", ".ai-bridge/plan", "name//file", "file name", "a/" * 7 + "z"])
def test_discard_rejects_bad_target_path(registry_fixture, monkeypatch, relative_path):
    _, path, params = _setup(registry_fixture, monkeypatch)
    params["relative_path"] = relative_path
    with pytest.raises(CandidateCloneError, match="relative_path"):
        discard_untracked_candidate_file(**params)
    assert path.read_bytes() == b"abandoned scratch\n"


def test_discard_requires_external_reference_guard_and_preserves_file(registry_fixture, monkeypatch):
    _, path, params = _setup(registry_fixture, monkeypatch)

    def active_reference():
        raise CandidateCloneError("WORKSPACE_CONTENDED", "active ref")

    params["reference_guard"] = active_reference
    with pytest.raises(CandidateCloneError, match="active ref"):
        discard_untracked_candidate_file(**params)
    assert path.read_bytes() == b"abandoned scratch\n"
    assert not list((params["journal_root"] / "candidate-file-archive").glob("*.blob"))


def test_discard_detects_writer_race_after_quarantine_and_restores_path(
    registry_fixture, monkeypatch
):
    from examples.mcp_server import candidate_file_discard as module

    _, path, params = _setup(registry_fixture, monkeypatch)
    original_rename = module.os.rename
    observed = []

    def concurrent_change(src, dst, *args, **kwargs):
        result = original_rename(src, dst, *args, **kwargs)
        if src == "scratch.txt":
            observed.append(dst)
            fd = os.open(dst, os.O_WRONLY, dir_fd=kwargs["dst_dir_fd"])
            try:
                os.write(fd, b"abandoned CHANGED!")
            finally:
                os.close(fd)
        return result

    monkeypatch.setattr(module.os, "rename", concurrent_change)
    with pytest.raises(CandidateCloneError, match="changed"):
        discard_untracked_candidate_file(**params)

    assert len(observed) == 1
    assert path.is_file()
    assert path.read_bytes() == b"abandoned CHANGED!"
    archive = params["journal_root"] / "candidate-file-archive"
    assert [p.read_bytes() for p in archive.glob("*.blob")] == [b"abandoned scratch\n"]
    assert not list(path.parent.glob(".mcp-discard-*"))


def test_discard_archive_collision_keeps_original_bytes(registry_fixture, monkeypatch):
    _, path, params = _setup(registry_fixture, monkeypatch)
    first = discard_untracked_candidate_file(**params)
    path.write_bytes(b"abandoned scratch\n")
    archive = params["journal_root"] / "candidate-file-archive" / (first["archive_id"] + ".blob")
    archive.write_bytes(b"evil but same length")
    with pytest.raises(CandidateCloneError, match="archive"):
        discard_untracked_candidate_file(**params)
    assert path.read_bytes() == b"abandoned scratch\n"
