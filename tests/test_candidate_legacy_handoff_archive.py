"""Legacy handoff archival is an explicit, provenance-fenced prerequisite to cleanup."""

from __future__ import annotations

import hashlib

import pytest

from examples.mcp_server import candidate_clone as core
from examples.mcp_server.candidate_clone import (
    CandidateCloneError,
    candidate_cleanup,
    prepare_candidate_clone,
)
from examples.mcp_server.legacy_candidate_handoff import archive_terminal_legacy_handoff
from tests.test_candidate_clone import _mock_cleanup_remote_probe

pytest_plugins = ("tests.test_candidate_clone",)


def _prepared(registry_fixture, monkeypatch):
    workspace, _source, config_dir, journal_root, head = registry_fixture
    branch = "fix/legacy-terminal-plan"
    preserved = "archive/candidate-legacy-terminal-plan"
    receipt = prepare_candidate_clone(
        "source-project", branch, head, config_dir=config_dir, journal_root=journal_root
    )
    clone = workspace / ".mcp-candidate-clones" / receipt.project_id
    bridge = clone / ".ai-bridge"
    bridge.mkdir()
    (bridge / "current-plan.md").write_bytes(b"# Historic task\nKeep this plan.\n")
    (bridge / "agent-status.md").write_bytes(b"Historic status and research.\n")
    monkeypatch.setattr(core, "_status_state", lambda _root: (False, "0" * 64, 0))
    _mock_cleanup_remote_probe(
        monkeypatch, core, preserved_ref=preserved, head=head, delivery_branch=branch
    )
    return clone, receipt, config_dir, journal_root, head, branch, preserved


def _archive(parts, **extra):
    clone, receipt, config_dir, journal_root, head, branch, preserved = parts
    terminal_guard = extra.pop("terminal_guard", lambda: None)
    return archive_terminal_legacy_handoff(
        receipt.project_id,
        head,
        branch,
        "source-project",
        preserved,
        hashlib.sha256(b"# Historic task\nKeep this plan.\n").hexdigest(),
        "Merged PR verified and obsolete handoff explicitly reconciled",
        config_dir=config_dir,
        journal_root=journal_root,
        terminal_guard=terminal_guard,
        **extra,
    )


def test_verified_legacy_handoff_archive_preserves_both_files_and_allows_cleanup(
    registry_fixture, monkeypatch
):
    parts = _prepared(registry_fixture, monkeypatch)
    clone, receipt, config_dir, journal_root, head, branch, preserved = parts
    result = _archive(parts)
    assert result["outcome"] == "archived"
    assert (
        result["plan_sha256"] == hashlib.sha256(b"# Historic task\nKeep this plan.\n").hexdigest()
    )
    assert not (clone / ".ai-bridge" / "current-plan.md").exists()
    assert not (clone / ".ai-bridge" / "agent-status.md").exists()
    assert (
        result["archived_status_sha256"]
        == hashlib.sha256(b"Historic status and research.\n").hexdigest()
    )
    assert _archive(parts)["outcome"] == "already_archived"
    cleaned = candidate_cleanup(
        receipt.project_id,
        head,
        branch,
        "source-project",
        preserved,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=lambda: None,
    )
    assert cleaned.directory_removed and cleaned.registry_removed
    assert not clone.exists()
    # Archive must survive the candidate clone's deletion.
    assert (journal_root / "candidate-cleanup" / result["archive_name"]).is_file()


@pytest.mark.parametrize("fault", ["plan_mismatch", "tasks", "symlink", "guard", "extra"])
def test_archive_fails_closed_without_touching_legacy_plan(registry_fixture, monkeypatch, fault):
    parts = _prepared(registry_fixture, monkeypatch)
    clone, _receipt, _config, _journal, _head, _branch, _preserved = parts
    bridge = clone / ".ai-bridge"
    opts = {}
    if fault == "plan_mismatch":
        (bridge / "current-plan.md").write_bytes(b"modified plan\n")
    elif fault == "tasks":
        (bridge / "tasks" / "live").mkdir(parents=True)
    elif fault == "symlink":
        old = bridge / "current-plan.md"
        old.rename(bridge / "stored-plan.md")
        old.symlink_to(bridge / "stored-plan.md")
    elif fault == "guard":

        def deny():
            raise CandidateCloneError("WORKSPACE_CONTENDED", "active worker")

        opts["terminal_guard"] = deny
    else:
        (bridge / "unexpected-evidence").write_text("preserve me\n", encoding="utf-8")
    with pytest.raises(CandidateCloneError):
        _archive(parts, **opts)
    assert (bridge / "current-plan.md").exists() or (bridge / "current-plan.md").is_symlink()


def test_archive_requires_remote_preservation_and_refuses_missing_delivery(
    registry_fixture, monkeypatch
):
    parts = _prepared(registry_fixture, monkeypatch)
    clone, _receipt, _config, _journal, head, branch, preserved = parts
    _mock_cleanup_remote_probe(
        monkeypatch,
        core,
        preserved_ref=preserved,
        head=head,
        delivery_branch=branch,
        preserved="absent",
    )
    with pytest.raises(CandidateCloneError):
        _archive(parts)
    assert (clone / ".ai-bridge" / "current-plan.md").is_file()


def test_interrupted_unlink_preserves_archived_status_and_can_resume(registry_fixture, monkeypatch):
    from examples.mcp_server import legacy_candidate_handoff as archive_module

    parts = _prepared(registry_fixture, monkeypatch)
    clone = parts[0]
    real_unlink = archive_module.os.unlink
    reject_once = True

    def interrupted_unlink(path, *, dir_fd=None):
        nonlocal reject_once
        if path == "agent-status.md" and reject_once:
            reject_once = False
            raise OSError("simulated interruption after plan unlink")
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(archive_module.os, "unlink", interrupted_unlink)
    with pytest.raises(OSError, match="simulated interruption"):
        _archive(parts)
    assert not (clone / ".ai-bridge" / "current-plan.md").exists()
    assert (clone / ".ai-bridge" / "agent-status.md").is_file()
    result = _archive(parts)
    assert result["outcome"] == "already_archived"
    assert (
        result["archived_status_sha256"]
        == hashlib.sha256(b"Historic status and research.\n").hexdigest()
    )
    assert not (clone / ".ai-bridge" / "agent-status.md").exists()


def test_replay_after_missing_status_preserves_durable_status_digest(registry_fixture, monkeypatch):
    parts = _prepared(registry_fixture, monkeypatch)
    clone = parts[0]
    original = _archive(parts)
    (clone / ".ai-bridge" / "current-plan.md").write_bytes(b"# Historic task\nKeep this plan.\n")
    replay = _archive(parts)
    assert replay["outcome"] == "archived"
    assert replay["archived_status_sha256"] == original["archived_status_sha256"]
    assert not (clone / ".ai-bridge" / "current-plan.md").exists()


def test_terminal_guard_denied_after_durable_archive_preserves_source(
    registry_fixture, monkeypatch
):
    parts = _prepared(registry_fixture, monkeypatch)
    clone, _, _, journal_root, _, _, _ = parts
    calls = 0

    def deny_second():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CandidateCloneError("WORKSPACE_CONTENDED", "lease appeared")

    with pytest.raises(CandidateCloneError, match="lease appeared"):
        _archive(parts, terminal_guard=deny_second)
    assert calls == 2
    assert (clone / ".ai-bridge" / "current-plan.md").is_file()
    assert (clone / ".ai-bridge" / "agent-status.md").is_file()
    assert list((journal_root / "candidate-cleanup").glob("*.legacy-handoff.json"))
    assert _archive(parts)["outcome"] == "archived"
    assert not (clone / ".ai-bridge" / "current-plan.md").exists()
