"""Required protected Gitea contexts may come from distinct workflow runs.

Real reproductions: Harvester #219, Mail Composer #125. Every required job
must bind to the exact workflow, run ID, attempt, SHA and stable actor receipt.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from examples.mcp_server.mcp_infra.adapters import remote

HEAD = "a" * 40
WORKFLOWS = [
    {"id": "ci.yml", "name": "content-harvester-service CI", "state": "active"},
    {"id": "runtime-image.yml", "name": "content-harvester runtime image", "state": "active"},
]
CONTEXTS = (
    "content-harvester-service CI / checks (pull_request)",
    "content-harvester-service CI / postgres-integration (pull_request)",
    "content-harvester runtime image / runtime-image (pull_request)",
    "content-harvester runtime image / image-handoff (pull_request)",
)


def make_run(run_id: int, conclusion: str = "success") -> dict:
    return {
        "id": run_id,
        "run_attempt": 1,
        "head_sha": HEAD,
        "event": "pull_request",
        "status": "completed",
        "conclusion": conclusion,
        "repository": {"name": "repo", "full_name": "owner/repo"},
        "actor": {"login": "ci-author"},
        "trigger_actor": {"login": "ci-trigger"},
    }


class MultiWorkflowClient:
    def __init__(self):
        self.runs = {101: make_run(101), 102: make_run(102)}
        self.workflows = deepcopy(WORKFLOWS)
        self.workflow_ids = {"ci.yml": [101], "runtime-image.yml": [102]}
        self.job_names = {
            101: ["checks", "postgres-integration"],
            102: ["runtime-image", "image-handoff", "publish-image"],
        }
        self.requested_workflows = []
        self.exact_run_reads = {}
        self.mutate_rerun_after_jobs = False
        self.branch_reads = 0
        self.merge_calls = []
        self.merged = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get_pull_request(self, owner, repo, pull_number):
        assert (owner, repo, pull_number) == ("owner", "repo", 25)
        return {
            "number": 25,
            "state": "closed" if self.merged else "open",
            "merged": self.merged,
            "mergeable": True,
            "merge_commit_sha": "c" * 40 if self.merged else None,
            "head": {"sha": HEAD, "ref": "feature"},
            "base": {"sha": "b" * 40, "ref": "master"},
            "html_url": "https://git.example.test/owner/repo/pulls/25",
        }

    async def get_repo(self, owner, repo):
        assert (owner, repo) == ("owner", "repo")
        return {"default_branch": "master"}

    async def get_branch(self, owner, repo, branch):
        assert (owner, repo, branch) == ("owner", "repo", "master")
        self.branch_reads += 1
        return {
            "enable_status_check": True,
            "status_check_contexts": list(CONTEXTS),
        }

    async def compare_commits(self, owner, repo, *, base, head):
        assert (owner, repo) == ("owner", "repo")
        if base == "b" * 40 and head == HEAD:
            return {"total_commits": 1, "commits": [{"sha": HEAD}]}
        if base == HEAD and head == "b" * 40:
            return {"total_commits": 0, "commits": []}
        raise AssertionError("unexpected base or head")

    async def merge_pull_request(self, owner, repo, pull_number, **kwargs):
        assert (owner, repo, pull_number) == ("owner", "repo", 25)
        self.merge_calls.append(kwargs)
        self.merged = True
        return {}

    async def list_workflows(self, owner, repo):
        assert (owner, repo) == ("owner", "repo")
        return {"total_count": len(self.workflows), "workflows": deepcopy(self.workflows)}

    async def list_action_runs(self, owner, repo, *, status, limit, page):
        assert (owner, repo, status, limit, page) == ("owner", "repo", None, 50, 1)
        runs = sorted(self.runs.values(), key=lambda x: x["id"], reverse=True)
        return {"total_count": len(runs), "workflow_runs": deepcopy(runs)}

    async def list_pull_request_workflow_runs(self, owner, repo, workflow_id):
        assert (owner, repo) == ("owner", "repo")
        self.requested_workflows.append(workflow_id)
        ids = self.workflow_ids.get(workflow_id, [])
        return {
            "total_count": len(ids),
            "workflow_runs": [deepcopy(self.runs[i]) for i in ids],
        }

    async def get_action_run(self, owner, repo, run_id):
        assert (owner, repo) == ("owner", "repo")
        self.exact_run_reads[run_id] = self.exact_run_reads.get(run_id, 0) + 1
        run = deepcopy(self.runs[run_id])
        if self.mutate_rerun_after_jobs and self.exact_run_reads[run_id] >= 2:
            run["run_attempt"] += 1
        return run

    async def list_action_run_jobs(self, owner, repo, run_id):
        assert (owner, repo) == ("owner", "repo")
        jobs = [
            {
                "id": run_id * 100 + n,
                "run_id": run_id,
                "run_attempt": self.runs[run_id]["run_attempt"],
                "head_sha": HEAD,
                "name": name,
                "status": "completed",
                "conclusion": "skipped" if name == "publish-image" else "success",
            }
            for n, name in enumerate(self.job_names[run_id], start=1)
        ]
        return {"total_count": len(jobs), "jobs": jobs}


async def evidence(client):
    return await remote._gitea_ci_evidence(
        client, "owner", "repo", HEAD,
        required_contexts=CONTEXTS,
    )


@pytest.mark.asyncio
async def test_split_workflow_provenance_accepted_only_when_all_required_jobs_green():
    client = MultiWorkflowClient()
    assert await evidence(client) == "CI_GREEN"
    assert set(client.requested_workflows) == {"ci.yml", "runtime-image.yml"}


@pytest.mark.asyncio
async def test_wrong_workflow_context_cannot_borrow_same_named_job():
    client = MultiWorkflowClient()
    # Correct job name, but delivered through a different workflow's run.
    client.workflow_ids["ci.yml"] = [102]
    client.job_names[102] = [
        "checks", "postgres-integration", "runtime-image", "image-handoff"
    ]
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_missing_required_workflow_fails_closed():
    client = MultiWorkflowClient()
    client.workflows = [WORKFLOWS[0]]
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_required_job_skipped_in_one_workflow_blocks_merge():
    client = MultiWorkflowClient()
    client.job_names[101] = ["checks"]
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_rerun_after_job_evidence_fails_closed():
    client = MultiWorkflowClient()
    client.mutate_rerun_after_jobs = True
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_failed_newer_exact_head_workflow_does_not_borrow_older_success():
    client = MultiWorkflowClient()
    client.runs[103] = make_run(103, "failure")
    client.workflow_ids["ci.yml"] = [103, 101]
    assert await evidence(client) == "CI_NOT_GREEN"


@pytest.mark.asyncio
async def test_wrong_job_attempt_fails_closed():
    client = MultiWorkflowClient()
    original = client.list_action_run_jobs

    async def wrong_attempt(owner, repo, run_id):
        x = await original(owner, repo, run_id)
        if run_id == 101:
            x["jobs"][0]["run_attempt"] = 2
        return x

    client.list_action_run_jobs = wrong_attempt
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_different_sha_on_required_workflow_fails_closed():
    client = MultiWorkflowClient()
    client.runs[101]["head_sha"] = "b" * 40
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_new_workflow_run_after_jobs_cannot_authorize_stale_contexts():
    client = MultiWorkflowClient()
    original = client.list_pull_request_workflow_runs
    ci_reads = 0

    async def changes_after_jobs(owner, repo, workflow_id):
        nonlocal ci_reads
        payload = await original(owner, repo, workflow_id)
        if workflow_id == "ci.yml":
            ci_reads += 1
            if ci_reads == 2:
                payload["workflow_runs"].insert(0, make_run(103, "failure"))
                payload["total_count"] += 1
        return payload

    client.list_pull_request_workflow_runs = changes_after_jobs
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_duplicate_workflow_names_cannot_bind_required_context():
    client = MultiWorkflowClient()
    client.workflows.append(
        {"id": "other.yml", "name": WORKFLOWS[0]["name"], "state": "active"}
    )
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_workflow_specific_client_uses_bounded_pr_endpoint(monkeypatch):
    from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

    seen = []
    raw = make_run(101)
    raw["actor"] = {"login": "ci-author", "email": "secret@example.invalid"}

    async def fake_get(self, endpoint, params=None, **path_params):
        seen.append((endpoint, params, path_params))
        return {"total_count": 1, "workflow_runs": [raw]}

    monkeypatch.setattr(GiteaClient, "_get", fake_get)
    client = GiteaClient("test-token")
    try:
        payload = await client.list_pull_request_workflow_runs(
            "owner", "repo", "ci.yml"
        )
    finally:
        await client.aclose()

    assert payload["total_count"] == 1
    assert payload["workflow_runs"][0]["actor"] == {"login": "ci-author"}
    assert "secret@example.invalid" not in str(payload)
    assert seen == [(
        "/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
        {"page": 1, "limit": 50, "event": "pull_request"},
        {"owner": "owner", "repo": "repo", "workflow_id": "ci.yml"},
    )]


@pytest.mark.asyncio
async def test_workflow_specific_client_rejects_untrusted_id_and_incomplete_page(
    monkeypatch,
):
    from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

    async def fake_get(self, endpoint, params=None, **path_params):
        return {"workflow_runs": [make_run(101)]}  # no total_count

    monkeypatch.setattr(GiteaClient, "_get", fake_get)
    client = GiteaClient("test-token")
    try:
        with pytest.raises(ValueError):
            await client.list_pull_request_workflow_runs(
                "owner", "repo", "../another.yml"
            )
        with pytest.raises(ValueError):
            await client.list_pull_request_workflow_runs(
                "owner", "repo", "ci.yml"
            )
    finally:
        await client.aclose()


def _make_full_merge_client():
    client = MultiWorkflowClient()
    return client, client, "b" * 40


@pytest.mark.asyncio
async def test_full_merge_gate_accepts_split_workflow_only_after_two_ci_checks(
    monkeypatch,
):
    client, witness, base_sha = _make_full_merge_client()
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request(
        "owner", "repo", 25, HEAD, expected_base_sha=base_sha
    )
    assert result["ok"] is True
    assert len(client.merge_calls) == 1
    assert witness.requested_workflows.count("ci.yml") == 4
    assert witness.requested_workflows.count("runtime-image.yml") == 4


@pytest.mark.asyncio
async def test_full_merge_gate_refuses_second_check_context_drift_before_post(
    monkeypatch,
):
    client, witness, base_sha = _make_full_merge_client()
    original_branch = client.get_branch

    async def change_required_job_after_first_gate(owner, repo, branch):
        reply = await original_branch(owner, repo, branch)
        if client.branch_reads == 2:
            witness.job_names[101] = ["checks"]  # postgres context disappears
        return reply

    client.get_branch = change_required_job_after_first_gate
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    result = await remote.gitea_merge_pull_request(
        "owner", "repo", 25, HEAD, expected_base_sha=base_sha
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "CI_EVIDENCE_INCOMPLETE"
    assert client.merge_calls == []
