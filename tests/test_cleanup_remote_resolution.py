from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server import agent_sources, control_plane_git
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    _resolve_trusted_remote,
    cleanup_remote_resolution_scope,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _source(root: Path) -> Path:
    root.mkdir()
    _git(root, "init")
    _git(root, "remote", "add", "gitea", "ssh://git@gitea/owner/project.git")
    return root


@pytest.fixture
def trusted_source(tmp_path, monkeypatch):
    root = _source(tmp_path / "source")
    monkeypatch.setenv("GITEA_TOKEN", "fixture-token-one")
    monkeypatch.setenv("GITEA_API_BASE", "http://gitea:3000/api/v1")
    calls = []

    def target(owner, repo, *, token):
        calls.append((owner, repo, token))
        return "operator", f"http://gitea:3000/{owner}/{repo}.git"

    monkeypatch.setattr(control_plane_git, "_repo_https_target", target)
    return root, calls


def test_unscoped_resolution_keeps_fresh_api_lookup(trusted_source):
    root, calls = trusted_source
    _resolve_trusted_remote(root)
    _resolve_trusted_remote(root)
    assert len(calls) == 2


def test_scope_reuses_api_target_but_observes_repository_change(trusted_source):
    root, calls = trusted_source
    with cleanup_remote_resolution_scope():
        original = _resolve_trusted_remote(root)
        for _ in range(5):
            assert _resolve_trusted_remote(root) == original
        assert len(calls) == 1
        _git(root, "remote", "set-url", "gitea", "ssh://git@gitea/owner/changed.git")
        changed = _resolve_trusted_remote(root)
        assert changed[1] == "http://gitea:3000/owner/changed.git"
        assert len(calls) == 2


@pytest.mark.parametrize("change", ["foreign", "conflicting", "removed"])
def test_cached_target_does_not_bypass_current_remote_validation(trusted_source, change):
    root, calls = trusted_source
    with cleanup_remote_resolution_scope():
        _resolve_trusted_remote(root)
        if change == "foreign":
            _git(root, "remote", "set-url", "gitea", "https://foreign.invalid/owner/project.git")
        elif change == "conflicting":
            _git(root, "remote", "add", "second", "ssh://git@gitea/owner/other.git")
        else:
            _git(root, "remote", "remove", "gitea")
        with pytest.raises(ManagedSourceBundleError):
            _resolve_trusted_remote(root)
        assert len(calls) == 1


def test_cached_target_does_not_borrow_missing_credential(trusted_source, monkeypatch):
    root, calls = trusted_source
    with cleanup_remote_resolution_scope():
        _resolve_trusted_remote(root)
        monkeypatch.delenv("GITEA_TOKEN")
        with pytest.raises(ManagedSourceBundleError, match="GITEA_TOKEN"):
            _resolve_trusted_remote(root)
        assert len(calls) == 1


def test_changed_credential_gets_new_authenticated_target(trusted_source, monkeypatch):
    root, calls = trusted_source
    with cleanup_remote_resolution_scope():
        _resolve_trusted_remote(root)
        monkeypatch.setenv("GITEA_TOKEN", "fixture-token-two")
        assert _resolve_trusted_remote(root)[2] == "fixture-token-two"
        assert [call[2] for call in calls] == ["fixture-token-one", "fixture-token-two"]


def test_same_repository_at_another_registered_root_gets_its_own_target(trusted_source):
    root, calls = trusted_source
    other = _source(root.parent / "other")
    with cleanup_remote_resolution_scope():
        _resolve_trusted_remote(root)
        _resolve_trusted_remote(other)
        _resolve_trusted_remote(root)
        assert len(calls) == 2


def test_scope_discards_targets_after_exception(trusted_source):
    root, calls = trusted_source
    with pytest.raises(RuntimeError, match="stop cleanup"):
        with cleanup_remote_resolution_scope():
            _resolve_trusted_remote(root)
            raise RuntimeError("stop cleanup")
    _resolve_trusted_remote(root)
    with cleanup_remote_resolution_scope():
        _resolve_trusted_remote(root)
        _resolve_trusted_remote(root)
    assert len(calls) == 3


def test_nested_scope_restores_the_outer_operation(trusted_source):
    root, calls = trusted_source
    with cleanup_remote_resolution_scope():
        _resolve_trusted_remote(root)
        with cleanup_remote_resolution_scope():
            _resolve_trusted_remote(root)
        _resolve_trusted_remote(root)
    assert len(calls) == 2


async def test_scope_propagates_into_cleanup_worker_thread(trusted_source):
    root, calls = trusted_source
    with cleanup_remote_resolution_scope():
        expected = _resolve_trusted_remote(root)
        assert await asyncio.to_thread(_resolve_trusted_remote, root) == expected
    assert len(calls) == 1


async def test_concurrent_cleanup_requests_do_not_share_targets(trusted_source):
    root, calls = trusted_source

    async def request():
        with cleanup_remote_resolution_scope():
            _resolve_trusted_remote(root)
            await asyncio.sleep(0)
            _resolve_trusted_remote(root)

    await asyncio.gather(request(), request())
    assert len(calls) == 2


def test_failed_authenticated_lookup_is_not_cached(trusted_source, monkeypatch):
    root, calls = trusted_source
    original = control_plane_git._repo_https_target
    attempts = 0

    def flaky_target(owner, repo, *, token):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary API failure")
        return original(owner, repo, token=token)

    monkeypatch.setattr(control_plane_git, "_repo_https_target", flaky_target)
    with cleanup_remote_resolution_scope():
        with pytest.raises(ManagedSourceBundleError):
            _resolve_trusted_remote(root)
        _resolve_trusted_remote(root)
        _resolve_trusted_remote(root)
    assert attempts == 2
    assert len(calls) == 1


async def test_public_cleanup_shares_target_across_live_guard_checkpoints(
    trusted_source, monkeypatch
):
    from examples.mcp_server.candidate_clone import CandidateCleanupReceipt
    from examples.mcp_server.mcp_infra.adapters import supervisor

    root, calls = trusted_source
    reference_checks = []

    async def run_tool(**kwargs):
        return await kwargs["fn"]()

    async def assert_no_open_reference(**kwargs):
        reference_checks.append(kwargs)

    class Audit:
        def append_required(self, event):
            pass

        def append(self, event):
            pass

    def core(project_id, head, branch, source_project, preserved_ref, **kwargs):
        # Exercise the adapter's actual worker-thread and reference-guard
        # composition, with no filesystem removal or network ref probes.
        for _ in range(2):
            agent_sources._resolve_trusted_remote(root)
            agent_sources._resolve_trusted_remote(root)
            kwargs["reference_guard"]()
        return CandidateCleanupReceipt(
            project_id, source_project, branch, head, preserved_ref,
            False, False, True, "fixture-tombstone",
        )

    monkeypatch.setattr(supervisor, "run_tool_async", run_tool)
    monkeypatch.setattr(supervisor, "_resolve_project", lambda _: ("demo", root))
    monkeypatch.setattr(supervisor, "_resolve_registry_config_dir", lambda: root.parent / "config")
    monkeypatch.setattr(supervisor, "_journal_root_for_project", lambda *args: root.parent / "journal")
    monkeypatch.setattr(supervisor, "_reset_project_registry_caches", lambda: True)
    monkeypatch.setattr(supervisor, "_assert_no_open_candidate_pr_reference", assert_no_open_reference)
    monkeypatch.setattr(supervisor, "server_attr", lambda name: lambda: Audit())
    monkeypatch.setattr(supervisor, "_candidate_cleanup", core)

    result = await supervisor.candidate_cleanup(
        "candidate-fixture", "a" * 40, "fix/example", "demo", "archive/candidate-fixture"
    )

    assert result["ok"] is True
    assert len(calls) == 1
    assert len(reference_checks) == 2
