"""Exact trusted push contexts and independent PR source checks must coexist."""
from __future__ import annotations

from copy import deepcopy

import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_infra.adapters import remote

HEAD = "f32379209b1cad41bd5866f92c340f9f245d66bb"
JOBS = ("verify", "ui", "image-postgres", "postgres-recovery", "harvester-integration")
CONTEXTS = tuple(f"ci / {job} (push)" for job in JOBS)


def run(run_id, event, attempt):
    return {
        "id": run_id, "event": event, "run_attempt": attempt, "head_sha": HEAD,
        "status": "completed", "conclusion": "success",
        "repository": {"name": "repo", "full_name": "owner/repo"},
        "actor": {"login": "ci-author"}, "trigger_actor": {"login": "ci-trigger"},
    }


class PairedClient:
    def __init__(self):
        self.runs = {29431: run(29431, "pull_request", 1), 29430: run(29430, "push", 2)}
        self.jobs = {
            rid: [
                {"id": rid * 100 + i, "run_id": rid, "run_attempt": item["run_attempt"],
                 "head_sha": HEAD, "name": name, "status": "completed",
                 "conclusion": "skipped" if rid == 29431 and i >= 2 else "success"}
                for i, name in enumerate(JOBS)
            ] for rid, item in self.runs.items()
        }
        self.workflow_runs = {"push": [29430], "pull_request": [29431]}
        self.lookups = []
        self.exact_reads = []
        self.merged = False
        self.merge_calls = []
        self.statuses = [
            {"id": 21, "context": "ci / verify (pull_request)", "state": "success"},
            *(
                {"id": 20 - i, "context": context, "state": "success"}
                for i, context in enumerate(CONTEXTS)
            ),
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get_pull_request(self, owner, repo, pull_number):
        return {"number": pull_number, "state": "closed" if self.merged else "open",
                "merged": self.merged, "mergeable": True,
                "merge_commit_sha": "c" * 40 if self.merged else None,
                "head": {"sha": HEAD, "ref": "feature"},
                "base": {"sha": "b" * 40, "ref": "master"},
                "html_url": "https://git.example.test/owner/repo/pulls/25"}

    async def get_repo(self, owner, repo):
        return {"default_branch": "master"}

    async def get_branch(self, owner, repo, branch):
        return {"enable_status_check": True, "status_check_contexts": list(CONTEXTS)}

    async def compare_commits(self, owner, repo, *, base, head):
        if base == "b" * 40 and head == HEAD:
            return {"total_commits": 1, "commits": [{"sha": HEAD}]}
        if base == HEAD and head == "b" * 40:
            return {"total_commits": 0, "commits": []}
        raise AssertionError("unexpected comparison")

    async def merge_pull_request(self, owner, repo, pull_number, **kwargs):
        self.merge_calls.append(kwargs)
        self.merged = True
        return {}

    async def list_workflows(self, owner, repo):
        return {"total_count": 1, "workflows": [{"id": "ci.yml", "name": "ci"}]}

    async def list_action_runs(self, owner, repo, *, status, limit, page):
        items = sorted(self.runs.values(), key=lambda r: r["id"], reverse=True)
        return {"total_count": len(items), "workflow_runs": deepcopy(items)}

    async def list_pull_request_workflow_runs(self, owner, repo, workflow_id):
        return await self.list_ci_workflow_runs(owner, repo, workflow_id, event="pull_request")

    async def list_ci_workflow_runs(self, owner, repo, workflow_id, *, event):
        assert (owner, repo, workflow_id) == ("owner", "repo", "ci.yml")
        self.lookups.append(event)
        items = [self.runs[rid] for rid in self.workflow_runs[event]]
        return {"total_count": len(items), "workflow_runs": deepcopy(items)}

    async def get_action_run(self, owner, repo, run_id):
        self.exact_reads.append(run_id)
        return deepcopy(self.runs[run_id])

    async def list_action_run_jobs(self, owner, repo, run_id):
        jobs = self.jobs[run_id]
        return {"total_count": len(jobs), "jobs": deepcopy(jobs)}

    async def list_commit_statuses(self, owner, repo, sha, *, page, limit):
        assert sha == HEAD and page == 1
        return deepcopy(self.statuses)


async def evidence(client, contexts=CONTEXTS):
    return await remote._gitea_ci_evidence(
        client, "owner", "repo", HEAD, required_contexts=contexts
    )


@pytest.mark.asyncio
async def test_exact_analytics_push_attempt_two_with_pr_source_success():
    client = PairedClient()
    assert await evidence(client) == "CI_GREEN"
    assert client.lookups.count("push") >= 2
    assert 29430 in client.exact_reads and 29431 in client.exact_reads


@pytest.mark.asyncio
async def test_mixed_events_are_independently_attested():
    client = PairedClient()
    assert await evidence(client, (*CONTEXTS, "ci / verify (pull_request)")) == "CI_GREEN"
    assert set(client.lookups) == {"push", "pull_request"}


@pytest.mark.asyncio
@pytest.mark.parametrize("defect,expected", [
    ("missing-pr", "CI_TRIGGER_INCOMPATIBLE"),
    ("failed-pr", "CI_NOT_GREEN"),
    ("wrong-event", "CI_EVIDENCE_INCOMPLETE"),
    ("wrong-sha", "CI_EVIDENCE_INCOMPLETE"),
    ("wrong-repo", "CI_EVIDENCE_INCOMPLETE"),
    ("stale-job", "CI_EVIDENCE_INCOMPLETE"),
    ("missing-job", "CI_EVIDENCE_INCOMPLETE"),
    ("duplicate-job", "CI_EVIDENCE_INCOMPLETE"),
    ("skipped-job", "CI_NOT_GREEN"),
    ("failed-job", "CI_NOT_GREEN"),
    ("failed-push", "CI_NOT_GREEN"),
    ("newer-failed-push", "CI_NOT_GREEN"),
])
async def test_push_context_fail_closed(defect, expected):
    client = PairedClient()
    if defect == "missing-pr":
        del client.runs[29431]
    elif defect == "failed-pr":
        client.runs[29431]["conclusion"] = "failure"
    elif defect == "wrong-event":
        client.workflow_runs["push"] = [29431]
    elif defect == "wrong-sha":
        client.runs[29430]["head_sha"] = "b" * 40
    elif defect == "wrong-repo":
        client.runs[29430]["repository"]["full_name"] = "foreign/repo"
    elif defect == "stale-job":
        client.jobs[29430][0]["run_attempt"] = 1
    elif defect == "missing-job":
        client.jobs[29430].pop()
    elif defect == "duplicate-job":
        client.jobs[29430].append(deepcopy(client.jobs[29430][0]))
    elif defect == "skipped-job":
        client.jobs[29430][0]["conclusion"] = "skipped"
    elif defect == "failed-job":
        client.jobs[29430][0]["conclusion"] = "failure"
    elif defect == "failed-push":
        client.runs[29430]["conclusion"] = "failure"
    elif defect == "newer-failed-push":
        client.runs[29432] = run(29432, "push", 1)
        client.runs[29432]["conclusion"] = "failure"
        client.workflow_runs["push"].insert(0, 29432)
    assert await evidence(client) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("state,expected", [
    ("pending", "CI_NOT_GREEN"),
    ("failure", "CI_NOT_GREEN"),
    ("skipped", "CI_NOT_GREEN"),
    (None, "CI_EVIDENCE_INCOMPLETE"),
])
async def test_latest_required_push_status_is_independent_gate(state, expected):
    client = PairedClient()
    if state is None:
        client.statuses = [row for row in client.statuses if row["context"] != CONTEXTS[0]]
    else:
        client.statuses.insert(0, {"id": 22, "context": CONTEXTS[0], "state": state})
    assert await evidence(client) == expected


@pytest.mark.asyncio
async def test_push_workflow_must_exist_in_catalog():
    client = PairedClient()
    assert await evidence(client, ("foreign / verify (push)",)) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_status_changes_during_push_attestation_fail_closed():
    client = PairedClient()
    original = client.list_action_run_jobs

    async def drift(owner, repo, rid):
        payload = await original(owner, repo, rid)
        if rid == 29430:
            client.statuses.insert(
                0, {"id": 22, "context": CONTEXTS[0], "state": "pending"}
            )
        return payload

    client.list_action_run_jobs = drift
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_pr_source_must_have_actual_successful_jobs():
    client = PairedClient()
    for job in client.jobs[29431]:
        job["conclusion"] = "skipped"
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_unsupported_explicit_event_is_not_authorized():
    assert await evidence(PairedClient(), ("ci / verify (workflow_dispatch)",)) == (
        "CI_EVIDENCE_INCOMPLETE"
    )


@pytest.mark.asyncio
async def test_push_attempt_changes_after_job_read():
    client = PairedClient()
    original = client.list_action_run_jobs

    async def drift(owner, repo, rid):
        payload = await original(owner, repo, rid)
        if rid == 29430:
            client.runs[rid]["run_attempt"] += 1
        return payload

    client.list_action_run_jobs = drift
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_pr_source_attempt_changes_while_push_is_attested():
    client = PairedClient()
    original = client.list_action_run_jobs

    async def drift(owner, repo, rid):
        payload = await original(owner, repo, rid)
        if rid == 29430:
            client.runs[29431]["run_attempt"] += 1
        return payload

    client.list_action_run_jobs = drift
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_full_guarded_merge_uses_both_event_receipts(monkeypatch):
    client = PairedClient()
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    result = await remote.gitea_merge_pull_request(
        "owner", "repo", 25, HEAD, expected_base_sha="b" * 40
    )
    assert result["ok"] is True
    assert len(client.merge_calls) == 1
    assert client.lookups.count("push") == 4


@pytest.mark.asyncio
async def test_client_push_workflow_read_has_fixed_allowlisted_event(monkeypatch):
    calls = []

    async def fake_get(self, endpoint, params=None, **path_params):
        calls.append((endpoint, params, path_params))
        return {"total_count": 1, "workflow_runs": [run(29430, "push", 2)]}

    monkeypatch.setattr(GiteaClient, "_get", fake_get)
    client = GiteaClient("test-token")
    try:
        payload = await client.list_ci_workflow_runs("owner", "repo", "ci.yml", event="push")
        assert payload["workflow_runs"][0]["event"] == "push"
        with pytest.raises(ValueError):
            await client.list_ci_workflow_runs("owner", "repo", "ci.yml", event="workflow_dispatch")
    finally:
        await client.aclose()
    assert calls == [(
        "/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
        {"page": 1, "limit": 50, "event": "push"},
        {"owner": "owner", "repo": "repo", "workflow_id": "ci.yml"},
    )]
