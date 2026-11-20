"""Fail-closed exact-run job attestation for guarded Gitea PR merges."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from examples.mcp_server.mcp_infra.adapters import remote

HEAD = "a" * 40
REQUIRED = (
    "CI / Change scope (pull_request)",
    "CI / Checks 3.11 (pull_request)",
)
RECEIPT = (765, 2, "pull_request", HEAD, "completed", "success")


def _job(job_id: int, name: str, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "id": job_id,
        "run_id": 765,
        "run_attempt": 2,
        "head_sha": HEAD,
        "name": name,
        "status": "completed",
        "conclusion": "success",
    }
    result.update(overrides)
    return result


def _jobs(*jobs: dict[str, object], total_count: int | None = None) -> dict[str, object]:
    return {"total_count": len(jobs) if total_count is None else total_count, "jobs": list(jobs)}


def _client(jobs: object) -> AsyncMock:
    client = AsyncMock()
    client.list_action_run_jobs.return_value = jobs
    client.get_action_run.return_value = {
        "id": 765,
        "run_attempt": 2,
        "event": "pull_request",
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
    }
    return client


@pytest.mark.asyncio
async def test_required_job_attestation_accepts_only_exact_green_jobs_and_conditional_skips():
    client = _client(_jobs(
        _job(1, "Change scope"),
        _job(2, "Checks 3.11"),
        _job(3, "deploy", conclusion="skipped"),
    ))
    assert await remote._gitea_ci_job_attestation(
        client, "owner", "repo", RECEIPT, REQUIRED
    ) == "CI_GREEN"
    client.list_action_run_jobs.assert_awaited_once_with("owner", "repo", 765)
    client.get_action_run.assert_awaited_once_with("owner", "repo", 765)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("jobs", "expected"),
    [
        (_jobs(_job(1, "Change scope"), _job(2, "Checks 3.11", conclusion="skipped")), "CI_NOT_GREEN"),
        (_jobs(_job(1, "Change scope"), _job(2, "Checks 3.11", conclusion="failure")), "CI_NOT_GREEN"),
        (_jobs(_job(1, "Change scope"), _job(2, "Checks 3.11", status="in_progress", conclusion=None)), "CI_NOT_GREEN"),
        (_jobs(_job(1, "Change scope"), _job(2, "deploy", conclusion="skipped")), "CI_EVIDENCE_INCOMPLETE"),
        (_jobs(_job(1, "Change scope"), _job(2, "Checks 3.11"), total_count=3), "CI_EVIDENCE_INCOMPLETE"),
        (_jobs(_job(1, "Change scope"), _job(1, "Checks 3.11")), "CI_EVIDENCE_INCOMPLETE"),
        (_jobs(_job(1, "Change scope"), _job(2, "Checks 3.11", run_attempt=1)), "CI_EVIDENCE_INCOMPLETE"),
        (_jobs(_job(1, "Change scope"), _job(2, "Checks 3.11", run_id=766)), "CI_EVIDENCE_INCOMPLETE"),
        (_jobs(_job(1, "Change scope"), _job(2, "Checks 3.11", head_sha="b" * 40)), "CI_EVIDENCE_INCOMPLETE"),
        (_jobs(_job(1, "Change scope"), _job(2, "Checks 3.11", id=True)), "CI_EVIDENCE_INCOMPLETE"),
        (_jobs(), "CI_EVIDENCE_INCOMPLETE"),
        ({"total_count": 2, "jobs": "not-a-list"}, "CI_EVIDENCE_INCOMPLETE"),
    ],
)
async def test_required_job_attestation_rejects_wrong_or_incomplete_evidence(jobs, expected):
    client = _client(jobs)
    assert await remote._gitea_ci_job_attestation(
        client, "owner", "repo", RECEIPT, REQUIRED
    ) == expected


@pytest.mark.asyncio
async def test_required_job_attestation_refuses_rerun_during_job_evidence():
    client = _client(_jobs(_job(1, "Change scope"), _job(2, "Checks 3.11")))
    client.get_action_run.return_value["run_attempt"] = 3
    assert await remote._gitea_ci_job_attestation(
        client, "owner", "repo", RECEIPT, REQUIRED
    ) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_required_job_attestation_refuses_malformed_policy_contexts():
    client = _client(_jobs(_job(1, "Change scope"), _job(2, "Checks 3.11")))
    assert await remote._gitea_ci_job_attestation(
        client, "owner", "repo", RECEIPT, ("unbound-external-context",)
    ) == "CI_EVIDENCE_INCOMPLETE"
    client.list_action_run_jobs.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_job_attestation_refuses_provider_job_read_failure():
    client = _client(None)
    client.list_action_run_jobs.side_effect = RuntimeError("upstream read failed")
    assert await remote._gitea_ci_job_attestation(
        client, "owner", "repo", RECEIPT, REQUIRED
    ) == "CI_EVIDENCE_INCOMPLETE"
