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


def _setup_legacy_plan(registry_fixture, monkeypatch):
    root, scratch, params = _setup(registry_fixture, monkeypatch)
    bridge = root / ".ai-bridge"
    bridge.mkdir()
    path = bridge / "current-plan.md"
    scratch.rename(path)
    params["relative_path"] = ".ai-bridge/current-plan.md"
    return root, path, params


def test_discard_exact_idle_legacy_plan_before_delivery(registry_fixture, monkeypatch):
    from tests.test_candidate_clone import _git

    root, plan, params = _setup_legacy_plan(registry_fixture, monkeypatch)
    (plan.parent / "tasks").mkdir()
    (plan.parent / "archive").mkdir()
    before = plan.read_bytes()
    head = _git(root, "rev-parse", "HEAD")
    calls = []
    params["reference_guard"] = lambda: calls.append(True)
    receipt = discard_untracked_candidate_file(**params)
    assert calls == [True, True]
    assert receipt["discarded"]
    assert not plan.exists()
    assert _git(root, "status", "--porcelain") == ""
    assert _git(root, "rev-parse", "HEAD") == head
    archive = params["journal_root"] / "candidate-file-archive" / (receipt["archive_id"] + ".blob")
    assert archive.read_bytes() == before


@pytest.mark.parametrize("fault", [
    "hash", "size", "task", "archive", "status", "ignored_extra",
    "symlink", "hardlink", "tracked", "hidden_tracked", "other_source",
])
def test_legacy_plan_exception_preserves_other_evidence(registry_fixture, monkeypatch, fault):
    from tests.test_candidate_clone import _git

    root, plan, params = _setup_legacy_plan(registry_fixture, monkeypatch)
    before = plan.read_bytes()
    if fault == "hash":
        params["expected_sha256"] = "0" * 64
    elif fault == "size":
        params["expected_size"] += 1
    elif fault == "task":
        (plan.parent / "tasks" / "active").mkdir(parents=True)
    elif fault == "archive":
        (plan.parent / "archive" / "historic").mkdir(parents=True)
    elif fault == "status":
        (plan.parent / "agent-status.md").write_text("unfinished", encoding="utf-8")
    elif fault == "ignored_extra":
        (root / ".git" / "info" / "exclude").write_text(".ai-bridge/ignored.md\n", encoding="utf-8")
        (plan.parent / "ignored.md").write_text("preserve", encoding="utf-8")
    elif fault == "symlink":
        plan.rename(plan.parent / "real-plan")
        plan.symlink_to(plan.parent / "real-plan")
    elif fault == "hardlink":
        os.link(plan, root / ".git" / "hardlink")
    elif fault == "tracked":
        _git(root, "add", ".ai-bridge/current-plan.md")
    elif fault == "hidden_tracked":
        _git(root, "update-index", "--assume-unchanged", "README.md")
        (root / "README.md").write_text("preserve", encoding="utf-8")
    else:
        (root / "unfinished.py").write_text("preserve", encoding="utf-8")
    with pytest.raises(CandidateCloneError):
        discard_untracked_candidate_file(**params)
    assert plan.read_bytes() == before
    assert not list((params["journal_root"] / "candidate-file-archive").glob("*.blob"))


def test_legacy_plan_rechecks_idle_evidence_after_archive(registry_fixture, monkeypatch):
    _, plan, params = _setup_legacy_plan(registry_fixture, monkeypatch)
    before = plan.read_bytes()
    calls = []

    def guard():
        calls.append(True)
        if len(calls) == 2:
            (plan.parent / "tasks" / "late").mkdir(parents=True)

    params["reference_guard"] = guard
    with pytest.raises(CandidateCloneError):
        discard_untracked_candidate_file(**params)
    assert plan.read_bytes() == before
    assert calls == [True, True]
    assert (plan.parent / "tasks" / "late").is_dir()


def test_discard_refuses_unsafe_archive_directory_without_deleting_file(
    registry_fixture, monkeypatch, tmp_path
):
    _, path, params = _setup(registry_fixture, monkeypatch)
    archive_dir = params["journal_root"] / "candidate-file-archive"
    outside = tmp_path / "untrusted"
    outside.mkdir()
    archive_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CandidateCloneError, match="archive unsafe"):
        discard_untracked_candidate_file(**params)

    assert path.read_bytes() == b"abandoned scratch\n"
    assert not list(outside.iterdir())


def test_discard_rejects_fifo_before_open_without_hanging(
    registry_fixture, monkeypatch
):
    _, path, params = _setup(registry_fixture, monkeypatch)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(CandidateCloneError, match="other changes|type"):
        discard_untracked_candidate_file(**params)
    assert path.is_fifo()


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


def test_discard_replay_after_success_preserves_recreated_identical_file(
    registry_fixture, monkeypatch
):
    _, path, params = _setup(registry_fixture, monkeypatch)
    first = discard_untracked_candidate_file(**params)
    assert first["discarded"]
    assert not path.exists()

    # Same path, HEAD, SHA and size, but it is a NEW file. A replay of the
    # previous ambiguous request must never silently delete it.
    path.write_bytes(b"abandoned scratch\n")
    with pytest.raises(CandidateCloneError, match="archive already exists"):
        discard_untracked_candidate_file(**params)
    assert path.read_bytes() == b"abandoned scratch\n"
    archive = (
        params["journal_root"] / "candidate-file-archive" /
        (first["archive_id"] + ".blob")
    )
    assert archive.read_bytes() == b"abandoned scratch\n"


def test_discard_archive_collision_keeps_original_bytes(registry_fixture, monkeypatch):
    _, path, params = _setup(registry_fixture, monkeypatch)
    first = discard_untracked_candidate_file(**params)
    path.write_bytes(b"abandoned scratch\n")
    archive = params["journal_root"] / "candidate-file-archive" / (first["archive_id"] + ".blob")
    archive.write_bytes(b"evil but same length")
    with pytest.raises(CandidateCloneError, match="archive"):
        discard_untracked_candidate_file(**params)
    assert path.read_bytes() == b"abandoned scratch\n"


@pytest.mark.parametrize("fail_parent_sync", [False, True])
def test_discard_syncs_archive_parent_before_original_rename(
    registry_fixture, monkeypatch, fail_parent_sync,
):
    import stat

    from examples.mcp_server import candidate_file_discard as module

    _, path, params = _setup(registry_fixture, monkeypatch)
    parent = params["journal_root"].stat()
    original_fsync = module.os.fsync
    synced = []

    def sync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            # The source must still exist and the exact archive must already be present.
            assert path.read_bytes() == b"abandoned scratch\n"
            archives = list((params["journal_root"] / "candidate-file-archive").glob("*.blob"))
            assert len(archives) == 1
            assert archives[0].read_bytes() == b"abandoned scratch\n"
            synced.append(True)
            if fail_parent_sync:
                raise OSError("directory fsync failed")
        return original_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", sync)
    if fail_parent_sync:
        with pytest.raises(CandidateCloneError) as exc_info:
            discard_untracked_candidate_file(**params)
        assert exc_info.value.code == "AUDIT_UNAVAILABLE"
        assert path.read_bytes() == b"abandoned scratch\n"
    else:
        assert discard_untracked_candidate_file(**params)["discarded"]
        assert not path.exists()
    assert synced


def test_discard_zero_progress_archive_write_fails_closed(registry_fixture, monkeypatch):
    from examples.mcp_server import candidate_file_discard as module

    _, path, params = _setup(registry_fixture, monkeypatch)
    original_write = module.os.write
    injected = False

    def write(fd, data):
        nonlocal injected
        if not injected and bytes(data) == b"abandoned scratch\n":
            injected = True
            return 0
        return original_write(fd, data)

    monkeypatch.setattr(module.os, "write", write)
    with pytest.raises(CandidateCloneError) as exc_info:
        discard_untracked_candidate_file(**params)
    assert injected
    assert exc_info.value.code == "AUDIT_UNAVAILABLE"
    assert path.read_bytes() == b"abandoned scratch\n"
