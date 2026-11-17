"""Gateway admin-only legacy-handoff archival must prove merged delivery and idle fleet."""

from __future__ import annotations

import asyncio

import pytest

from examples.mcp_server.mcp_infra.adapters import supervisor
from examples.mcp_server.tool_scopes import TOOL_SCOPES
from tests import test_mcp_supervisor_integration as helpers


@pytest.fixture
def direct_async(monkeypatch):
    async def run(*, tool, title, fn, success_text):
        del tool, title, success_text
        return await fn()

    monkeypatch.setattr(supervisor, "run_tool_async", run)


class _Fleet:
    def __init__(self, *, owner_loop, live=0, leases=None):
        self.owner_loop = owner_loop
        self.live = live
        self.leases = [] if leases is None else leases
        self.reads = 0

    async def fleet_status(self, *, job_status_fn):
        assert asyncio.get_running_loop() is self.owner_loop
        assert callable(job_status_fn)
        self.reads += 1
        return {
            "counts": {"live": self.live, "bound_status_unknown": 0},
            "leases": self.leases,
        }


def _configure(tmp_path, monkeypatch, *, merged=True, live=0, fleet_available=True):
    sha = "a" * 40
    client = helpers._PullClient(
        closed_pages={1: [helpers._merged_same_repo_pr(11, ref="candidate/demo", sha=sha)]}
        if merged
        else {}
    )
    audit = helpers._AuditLogger()
    calls = []
    helpers._configure_cleanup_adapter(
        tmp_path, monkeypatch, client=client, audit=audit, guard_calls=calls
    )
    fleet = _Fleet(owner_loop=asyncio.get_running_loop(), live=live)

    async def runtime():
        return fleet if fleet_available else None

    monkeypatch.setattr(supervisor, "get_fleet_runtime", runtime)
    archives = []

    def archive(*args, terminal_guard, **kwargs):
        archives.append((args, kwargs))
        terminal_guard()
        terminal_guard()
        return {
            "outcome": "archived",
            "archive_name": "test.legacy-handoff.json",
            "plan_sha256": "b" * 64,
            "archived_status_sha256": "c" * 64,
        }

    monkeypatch.setattr(supervisor, "_archive_terminal_legacy_handoff", archive)
    return client, audit, fleet, archives


@pytest.mark.asyncio
async def test_admin_archive_requires_explicit_acknowledgement(tmp_path, direct_async, monkeypatch):
    _, audit, fleet, archives = _configure(tmp_path, monkeypatch)
    result = await supervisor.candidate_archive_terminal_legacy_handoff(
        "candidate-demo",
        "a" * 40,
        "candidate/demo",
        "demo",
        "archive/candidate-demo",
        "b" * 64,
        "explicit terminal resolution",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert audit.required == []
    assert fleet.reads == 0
    assert archives == []


@pytest.mark.asyncio
async def test_admin_archive_requires_merged_delivery_and_two_fresh_idle_proofs(
    tmp_path, direct_async, monkeypatch
):
    client, audit, fleet, archives = _configure(tmp_path, monkeypatch)
    result = await supervisor.candidate_archive_terminal_legacy_handoff(
        "candidate-demo",
        "a" * 40,
        "candidate/demo",
        "demo",
        "archive/candidate-demo",
        "b" * 64,
        "explicit terminal resolution",
        acknowledge=True,
    )
    assert result["ok"] is True
    assert len(archives) == 1
    assert len(audit.required) == 1
    assert audit.required[0].metadata["delivery_pull_number"] == 11
    assert audit.required[0].metadata["delivery_merge_commit_sha"] == "c" * 40
    assert fleet.reads == 2
    assert client.closed_calls == [1, 1]
    assert client.calls == [1, 1]
    assert result["result"]["plan_sha256"] == "b" * 64
    assert TOOL_SCOPES["candidate_archive_terminal_legacy_handoff"] == ["mcp:admin"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("unmerged", "CHECK_FAILED"),
        ("live", "WORKSPACE_CONTENDED"),
        ("fleet_disabled", "CHECK_FAILED"),
    ],
)
async def test_archive_fails_closed_without_terminal_evidence(
    tmp_path, direct_async, monkeypatch, case, expected
):
    client, audit, _fleet, archives = _configure(
        tmp_path,
        monkeypatch,
        merged=case != "unmerged",
        live=1 if case == "live" else 0,
        fleet_available=case != "fleet_disabled",
    )
    result = await supervisor.candidate_archive_terminal_legacy_handoff(
        "candidate-demo",
        "a" * 40,
        "candidate/demo",
        "demo",
        "archive/candidate-demo",
        "b" * 64,
        "explicit terminal resolution",
        acknowledge=True,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == expected
    assert audit.required == []
    assert len(archives) == 1  # Core preflight reached, then the guard denied.
