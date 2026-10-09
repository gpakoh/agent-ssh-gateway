"""Guarded Gitea Actions single-job rerun write contract."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_infra.adapters import remote
from examples.mcp_server.tool_modes import MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS, TOOL_NAMES_BY_MODE
from examples.mcp_server.tool_scopes import get_required_scopes

SHA = "c7af1093d7b041d5efa0c1ac1bf18d51f167f5e7"
RUN_ID = 14840
JOB_ID = 62917
RERUN_JOB_ID = 70001
JOB_NAME = "Python 3.11"


def _run_payload(
    *,
    run_id: object = RUN_ID,
    head_sha: object = SHA,
    run_attempt: object = 1,
    status: object = "completed",
    conclusion: object = "failure",
) -> dict[str, object]:
    return {
        "id": run_id,
        "run_attempt": run_attempt,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "event": "pull_request",
        "html_url": f"https://git.example/actions/runs/{RUN_ID}",
    }


def _job_payload(
    *,
    job_id: object = JOB_ID,
    run_id: object = RUN_ID,
    run_attempt: object = 1,
    head_sha: object = SHA,
    name: object = JOB_NAME,
    status: object = "completed",
    conclusion: object = "failure",
) -> dict[str, object]:
    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "head_branch": None,
        "head_sha": head_sha,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "runner_id": 13,
        "runner_name": "runner-python311",
        "started_at": "2026-10-08T19:56:41Z",
        "completed_at": "2026-10-08T19:57:46Z" if status == "completed" else None,
        "url": f"https://git.example/api/v1/actions/jobs/{job_id}",
        "run_url": f"https://git.example/api/v1/actions/runs/{RUN_ID}",
    }


class FakeJobRerunClient:
    def __init__(
        self,
        token: str,
        *,
        before_run: dict[str, object] | None = None,
        after_run: dict[str, object] | None = None,
        before_job: dict[str, object] | None = None,
        after_job: dict[str, object] | None = None,
        current_jobs: list[dict[str, object]] | None = None,
        mutation_error: BaseException | None = None,
        apply_mutation: bool = True,
    ) -> None:
        assert token == "token"
        self.before_run = before_run or _run_payload()
        self.after_run = after_run or _run_payload(
            run_attempt=2,
            status="in_progress",
            conclusion=None,
        )
        self.before_job = before_job or _job_payload()
        self.after_job = after_job or _job_payload(
            job_id=RERUN_JOB_ID,
            run_attempt=2,
            status="in_progress",
            conclusion=None,
        )
        self.current_jobs = current_jobs
        self.mutation_error = mutation_error
        self.apply_mutation = apply_mutation
        self.mutated = False
        self.rerun_calls: list[tuple[str, str, int, int]] = []
        self.run_reads = 0
        self.job_reads = 0
        self.list_reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_action_run(self, owner: str, repo: str, run_id: int):
        self.run_reads += 1
        return dict(self.after_run if self.mutated else self.before_run)

    async def get_action_job(self, owner: str, repo: str, job_id: int):
        self.job_reads += 1
        return dict(self.before_job)

    async def list_action_run_jobs(self, owner: str, repo: str, run_id: int):
        self.list_reads += 1
        if self.mutated:
            jobs = [dict(self.after_job)]
        elif self.current_jobs is not None:
            jobs = [dict(job) for job in self.current_jobs]
        else:
            jobs = [dict(self.before_job)]
        return {"total_count": len(jobs), "jobs": jobs}

    async def rerun_action_job(self, owner: str, repo: str, run_id: int, job_id: int):
        self.rerun_calls.append((owner, repo, run_id, job_id))
        if self.apply_mutation:
            self.mutated = True
        if self.mutation_error is not None:
            raise self.mutation_error
        return dict(self.after_job)


def _setup(monkeypatch, client: FakeJobRerunClient) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)


@pytest.mark.asyncio
async def test_client_job_rerun_uses_only_dedicated_endpoint(monkeypatch):
    client = GiteaClient("token")
    post_action = AsyncMock(return_value=_job_payload(job_id=RERUN_JOB_ID, run_attempt=2))
    monkeypatch.setattr(client, "_post_action", post_action)
    try:
        result = await client.rerun_action_job("owner", "repo", RUN_ID, JOB_ID)
    finally:
        await client.aclose()

    assert result["id"] == RERUN_JOB_ID
    assert result["run_attempt"] == 2
    post_action.assert_awaited_once_with(
        "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}/rerun",
        owner="owner",
        repo="repo",
        run_id=RUN_ID,
        job_id=JOB_ID,
    )


@pytest.mark.asyncio
async def test_client_get_action_job_uses_exact_job_endpoint(monkeypatch):
    client = GiteaClient("token")
    get = AsyncMock(return_value=_job_payload())
    monkeypatch.setattr(client, "_get", get)
    try:
        result = await client.get_action_job("owner", "repo", JOB_ID)
    finally:
        await client.aclose()

    assert result["id"] == JOB_ID
    get.assert_awaited_once_with(
        "/repos/{owner}/{repo}/actions/jobs/{job_id}",
        owner="owner",
        repo="repo",
        job_id=JOB_ID,
    )


@pytest.mark.asyncio
async def test_client_job_rerun_rejects_bad_ids_before_post(monkeypatch):
    client = GiteaClient("token")
    post_action = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_post_action", post_action)
    try:
        for bad in (0, -1, True, "1"):
            with pytest.raises(ValueError):
                await client.rerun_action_job("owner", "repo", bad, JOB_ID)  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                await client.rerun_action_job("owner", "repo", RUN_ID, bad)  # type: ignore[arg-type]
    finally:
        await client.aclose()
    post_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_reruns_exact_current_failed_job_once(monkeypatch):
    client = FakeJobRerunClient("token")
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_job("owner", "repo", RUN_ID, JOB_ID, SHA)

    assert result["ok"] is True
    assert client.rerun_calls == [("owner", "repo", RUN_ID, JOB_ID)]
    assert result["result"]["selected_job_id"] == JOB_ID
    assert result["result"]["rerun_job_id"] == RERUN_JOB_ID
    assert result["result"]["job_name"] == JOB_NAME
    assert result["result"]["previous_attempt"] == 1
    assert result["result"]["run_attempt"] == 2
    assert result["result"]["verified"] is True
    assert result["result"]["reconciliation_attempts"] == 0


@pytest.mark.asyncio
async def test_adapter_run_head_mismatch_is_zero_mutation(monkeypatch):
    client = FakeJobRerunClient("token", before_run=_run_payload(head_sha="b" * 40))
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_job("owner", "repo", RUN_ID, JOB_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert client.rerun_calls == []


@pytest.mark.asyncio
async def test_adapter_job_must_belong_to_requested_run(monkeypatch):
    client = FakeJobRerunClient("token", before_job=_job_payload(run_id=RUN_ID + 1))
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_job("owner", "repo", RUN_ID, JOB_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "JOB_RUN_MISMATCH"
    assert client.rerun_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_stale_attempt_job(monkeypatch):
    client = FakeJobRerunClient(
        "token",
        before_run=_run_payload(run_attempt=2),
        before_job=_job_payload(run_attempt=1),
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_job("owner", "repo", RUN_ID, JOB_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "JOB_STATE_UNPROVEN"
    assert client.rerun_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_successful_job_without_mutation(monkeypatch):
    client = FakeJobRerunClient("token", before_job=_job_payload(conclusion="success"))
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_job("owner", "repo", RUN_ID, JOB_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "JOB_ALREADY_SUCCESSFUL"
    assert client.rerun_calls == []


@pytest.mark.asyncio
async def test_adapter_requires_unique_logical_job_name_before_mutation(monkeypatch):
    duplicate = _job_payload(job_id=JOB_ID + 1)
    client = FakeJobRerunClient(
        "token",
        current_jobs=[_job_payload(), duplicate],
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_job("owner", "repo", RUN_ID, JOB_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "JOB_IDENTITY_AMBIGUOUS"
    assert client.rerun_calls == []


@pytest.mark.asyncio
async def test_adapter_transport_loss_after_applied_job_rerun_reconciles(monkeypatch):
    request = httpx.Request(
        "POST",
        f"https://git.example/actions/runs/{RUN_ID}/jobs/{JOB_ID}/rerun",
    )
    client = FakeJobRerunClient(
        "token",
        mutation_error=httpx.ReadTimeout("lost response", request=request),
        apply_mutation=True,
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_job("owner", "repo", RUN_ID, JOB_ID, SHA)

    assert result["ok"] is True
    assert client.rerun_calls == [("owner", "repo", RUN_ID, JOB_ID)]
    assert result["result"]["outcome"] == "started_after_ambiguous_response"
    assert result["result"]["rerun_job_id"] == RERUN_JOB_ID
    assert result["result"]["run_attempt"] == 2
    assert result["result"]["reconciliation_attempts"] == 1


@pytest.mark.asyncio
async def test_adapter_unproven_ambiguous_job_rerun_forbids_blind_retry(monkeypatch):
    request = httpx.Request(
        "POST",
        f"https://git.example/actions/runs/{RUN_ID}/jobs/{JOB_ID}/rerun",
    )
    client = FakeJobRerunClient(
        "token",
        mutation_error=httpx.ReadTimeout("lost response", request=request),
        apply_mutation=False,
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_job("owner", "repo", RUN_ID, JOB_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert client.rerun_calls == [("owner", "repo", RUN_ID, JOB_ID)]
    assert client.list_reads == 4  # preflight + three bounded reconciliation reads


@pytest.mark.asyncio
async def test_adapter_requires_token_and_valid_job_inputs(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    missing = await remote.gitea_rerun_action_job("owner", "repo", RUN_ID, JOB_ID, SHA)
    assert missing["error"]["code"] == "DEPENDENCY_MISSING"

    monkeypatch.setenv("GITEA_TOKEN", "token")
    for bad_run_id in (0, True):
        bad = await remote.gitea_rerun_action_job(  # type: ignore[arg-type]
            "owner", "repo", bad_run_id, JOB_ID, SHA
        )
        assert bad["error"]["code"] == "INVALID_INPUT"
    for bad_job_id in (0, True):
        bad = await remote.gitea_rerun_action_job(  # type: ignore[arg-type]
            "owner", "repo", RUN_ID, bad_job_id, SHA
        )
        assert bad["error"]["code"] == "INVALID_INPUT"
    for bad_sha in ("abc", True, None):
        bad = await remote.gitea_rerun_action_job(  # type: ignore[arg-type]
            "owner", "repo", RUN_ID, JOB_ID, bad_sha
        )
        assert bad["error"]["code"] == "INVALID_INPUT"


def test_job_rerun_tool_is_write_admin_only():
    assert "gitea_rerun_action_job" in MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS
    assert "gitea_rerun_action_job" not in TOOL_NAMES_BY_MODE["mcp_client"]
    assert "gitea_rerun_action_job" in TOOL_NAMES_BY_MODE["mcp_client_write"]
    assert get_required_scopes("gitea_rerun_action_job") == ["mcp:repo", "mcp:admin"]
