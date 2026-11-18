"""An exact failed Actions run must have a terminal, complete job set before rerun.

This protects the shared CI runner from accidentally duplicating still-running jobs
when Gitea reports a terminal workflow before job settlement is authoritative.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from examples.mcp_server.mcp_infra.adapters import remote

SHA = "a" * 40
RUN_ID = 1234


def _run(*, attempt: int = 1, status: str = "completed", conclusion: str | None = "failure") -> dict:
    return {"id": RUN_ID, "head_sha": SHA, "run_attempt": attempt, "status": status, "conclusion": conclusion}


def _job(*, job_id: int = 11, attempt: object = 1, status: object = "completed",
         conclusion: object = "failure", run_id: object = RUN_ID, head_sha: object = SHA) -> dict:
    return {"id": job_id, "run_id": run_id, "head_sha": head_sha,
            "run_attempt": attempt, "status": status, "conclusion": conclusion}


class _Client:
    def __init__(self, *, jobs: object, before: dict | None = None, second: dict | None = None):
        self.jobs = jobs
        self.before = _run() if before is None else before
        self.second = self.before if second is None else second
        self.calls: list[str] = []
        self.reads = 0
        self.mutated = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_action_run(self, owner: str, repo: str, run_id: int):
        assert (owner, repo, run_id) == ("owner", "repo", RUN_ID)
        self.calls.append("get_run")
        self.reads += 1
        if self.mutated:
            return _run(attempt=int(self.before["run_attempt"]) + 1, status="in_progress", conclusion=None)
        return deepcopy(self.before if self.reads == 1 else self.second)

    async def list_action_run_jobs(self, owner: str, repo: str, run_id: int):
        assert (owner, repo, run_id) == ("owner", "repo", RUN_ID)
        self.calls.append("get_jobs")
        return deepcopy(self.jobs)

    async def rerun_action_run(self, owner: str, repo: str, run_id: int):
        assert (owner, repo, run_id) == ("owner", "repo", RUN_ID)
        self.calls.append("POST")
        self.mutated = True
        return _run(attempt=2, status="in_progress", conclusion=None)


def _attach(monkeypatch: pytest.MonkeyPatch, client: _Client):
    monkeypatch.setenv("GITEA_TOKEN", "test-only")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda _token: client)


@pytest.mark.asyncio
async def test_rerun_with_complete_exact_attempt_jobs_and_second_run_read(monkeypatch):
    jobs = {"total_count": 3, "jobs": [
        _job(job_id=1, attempt=1, conclusion="success"),
        _job(job_id=2, attempt=2, conclusion="failure"),
        _job(job_id=3, attempt=2, conclusion="skipped"),
    ]}
    before = _run(attempt=2)
    client = _Client(jobs=jobs, before=before)
    _attach(monkeypatch, client)

    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)

    assert result["ok"] is True
    assert result["result"]["previous_attempt"] == 2
    assert result["result"]["run_attempt"] == 3
    assert client.calls[:3] == ["get_run", "get_jobs", "get_run"]
    assert client.calls.count("POST") == 1


@pytest.mark.asyncio
async def test_active_job_blocks_rerun_even_when_run_claims_completed(monkeypatch):
    client = _Client(jobs={"total_count": 2, "jobs": [_job(), _job(job_id=12, status="in_progress", conclusion=None)]})
    _attach(monkeypatch, client)
    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)
    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_STATE_UNPROVEN", result
    assert result["error"]["details"]["reason"] == "jobs_nonterminal"
    assert result["error"]["details"]["mutation_started"] is False
    assert "POST" not in client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "jobs",
    [
        {"jobs": [_job()]},  # no pagination completeness proof
        {"total_count": 3, "jobs": [_job()]},  # a partial page
        {"total_count": 0, "jobs": []},
        {"total_count": 1, "jobs": [_job(run_id=999)]},
        {"total_count": 1, "jobs": [_job(head_sha="b" * 40)]},
        {"total_count": 1, "jobs": [_job(attempt=True)]},
        {"total_count": 1, "jobs": [_job(attempt=2)]},
        {"total_count": 1, "jobs": [_job(attempt=0)]},
        {"total_count": True, "jobs": [_job()]},
        {"total_count": 1, "jobs": [_job(job_id=True)]},
        {"total_count": 1, "jobs": [_job(status="mystery")]},
        {"total_count": 1, "jobs": [_job(conclusion=None)]},
        {"total_count": 2, "jobs": [_job(), _job()]},  # duplicate job identity
        {"total_count": 1, "jobs": "malformed"},
    ],
)
async def test_untrusted_or_incomplete_job_set_never_posts(monkeypatch, jobs):
    client = _Client(jobs=jobs)
    _attach(monkeypatch, client)
    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)
    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_STATE_UNPROVEN"
    assert result["error"]["details"]["mutation_started"] is False
    assert "POST" not in client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second",
    [
        _run(attempt=2),
        _run(status="in_progress", conclusion=None),
        _run(conclusion="success"),
        {**_run(), "head_sha": "b" * 40},
        {**_run(), "id": 4567},
    ],
)
async def test_concurrent_run_drift_between_jobs_and_post_fails_closed(monkeypatch, second):
    client = _Client(jobs={"total_count": 1, "jobs": [_job()]}, second=second)
    _attach(monkeypatch, client)
    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)
    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_STATE_UNPROVEN"
    assert result["error"]["details"]["mutation_started"] is False
    assert client.calls == ["get_run", "get_jobs", "get_run"]


@pytest.mark.asyncio
async def test_job_list_fetch_failure_is_not_rerun_authority(monkeypatch):
    class _Faulty(_Client):
        async def list_action_run_jobs(self, owner, repo, run_id):
            self.calls.append("get_jobs")
            raise TimeoutError("sensitive-token=do-not-reflect")

    client = _Faulty(jobs={})
    _attach(monkeypatch, client)
    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)
    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_STATE_UNPROVEN"
    assert "sensitive-token" not in str(result)
    assert "POST" not in client.calls


@pytest.mark.asyncio
async def test_old_attempt_still_active_blocks_full_run_rerun(monkeypatch):
    old_running = _job(job_id=6, attempt=1, status="in_progress", conclusion=None)
    current_done = _job(job_id=7, attempt=2)
    client = _Client(jobs={"total_count": 2, "jobs": [old_running, current_done]}, before=_run(attempt=2))
    _attach(monkeypatch, client)
    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)
    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_STATE_UNPROVEN"
    assert result["error"]["details"]["reason"] == "jobs_nonterminal"
    assert result["error"]["details"]["mutation_started"] is False
    assert "POST" not in client.calls
