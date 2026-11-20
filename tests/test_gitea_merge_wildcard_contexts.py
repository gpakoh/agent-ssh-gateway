"""Gitea required status patterns are globs, not always literal job contexts.

Clicker PR #244 real provider evidence:
- branch protection status_check_contexts=['*regression*']
- workflow ci.yml / name ci, PR run #27402 attempt 1
- job regression SUCCESS, matching current status success
- older duplicate pending rows and conditional push skipped status
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from examples.mcp_server.mcp_infra.adapters import remote

HEAD = "a" * 40
REQUIRED = ("*regression*",)


def green_run():
    return {
        "id": 27402,
        "run_attempt": 1,
        "event": "pull_request",
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
        "repository": {"name": "repo", "full_name": "owner/repo"},
        "actor": {"login": "ci-author"},
        "trigger_actor": {"login": "ci-operator"},
    }


def status(status_id, context, state):
    return {"id": status_id, "context": context, "state": state}


class WildcardClient:
    def __init__(self):
        self.run = green_run()
        self.job = {
            "id": 167399,
            "run_id": 27402,
            "run_attempt": 1,
            "head_sha": HEAD,
            "name": "regression",
            "status": "completed",
            "conclusion": "success",
        }
        self.statuses = [
            status(4, "ci / regression (pull_request)", "success"),
            status(3, "ci / regression (pull_request)", "pending"),
            status(2, "ci / regression (push)", "skipped"),
        ]
        self.mutate_status_after_first = False
        self.status_calls = 0
        self.mutate_actor_after_first = False
        self.run_calls = 0
        self.branch_reads = 0
        self.merged = False
        self.merge_calls = []

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
        return {"enable_status_check": True, "status_check_contexts": list(REQUIRED)}

    async def compare_commits(self, owner, repo, *, base, head):
        assert (owner, repo) == ("owner", "repo")
        if base == "b" * 40 and head == HEAD:
            return {"total_commits": 1, "commits": [{"sha": HEAD}]}
        if base == HEAD and head == "b" * 40:
            return {"total_commits": 0, "commits": []}
        raise AssertionError("unexpected SHA pair")

    async def merge_pull_request(self, owner, repo, pull_number, **kwargs):
        assert (owner, repo, pull_number) == ("owner", "repo", 25)
        self.merge_calls.append(kwargs)
        self.merged = True
        return {}

    async def list_workflows(self, owner, repo):
        return {
            "total_count": 1,
            "workflows": [{"name": "ci", "id": "ci.yml", "state": "active"}],
        }

    async def list_action_runs(self, owner, repo, *, status, limit, page):
        assert (owner, repo, status, limit, page) == ("owner", "repo", None, 50, 1)
        return {"total_count": 1, "workflow_runs": [deepcopy(self.run)]}

    async def list_pull_request_workflow_runs(self, owner, repo, workflow_id):
        assert (owner, repo, workflow_id) == ("owner", "repo", "ci.yml")
        return {"total_count": 1, "workflow_runs": [deepcopy(self.run)]}

    async def get_action_run(self, owner, repo, run_id):
        assert (owner, repo, run_id) == ("owner", "repo", 27402)
        self.run_calls += 1
        run = deepcopy(self.run)
        if self.mutate_actor_after_first and self.run_calls >= 2:
            run["trigger_actor"] = {"login": "rerun-operator"}
        return run

    async def list_action_run_jobs(self, owner, repo, run_id):
        assert (owner, repo, run_id) == ("owner", "repo", 27402)
        return {"total_count": 1, "jobs": [deepcopy(self.job)]}

    async def list_commit_statuses(self, owner, repo, sha, *, page, limit):
        assert (owner, repo, sha) == ("owner", "repo", HEAD)
        assert limit == 50 and page == 1
        self.status_calls += 1
        statuses = deepcopy(self.statuses)
        if self.mutate_status_after_first and self.status_calls > 1:
            statuses.insert(0, status(5, "ci / regression (pull_request)", "pending"))
        return statuses


async def evidence(client, required=REQUIRED):
    return await remote._gitea_ci_evidence(
        client, "owner", "repo", HEAD, required_contexts=required
    )


@pytest.mark.asyncio
async def test_real_clicker_pattern_attests_current_green_run_not_old_pending():
    assert await evidence(WildcardClient()) == "CI_GREEN"


@pytest.mark.asyncio
async def test_wildcard_never_uses_old_success_when_new_status_pending():
    client = WildcardClient()
    client.statuses.insert(
        0, status(5, "ci / regression (pull_request)", "pending")
    )
    assert await evidence(client) == "CI_NOT_GREEN"


@pytest.mark.asyncio
async def test_wildcard_requires_real_pr_context_not_push_only():
    client = WildcardClient()
    client.statuses = [status(2, "ci / regression (push)", "success")]
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_wildcard_rejects_wrong_job_attempt():
    client = WildcardClient()
    client.job["run_attempt"] = 2
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_wildcard_rejects_skipped_required_job():
    client = WildcardClient()
    client.job["conclusion"] = "skipped"
    assert await evidence(client) == "CI_NOT_GREEN"


@pytest.mark.asyncio
async def test_wildcard_rejects_status_race_during_job_attestation():
    client = WildcardClient()
    client.mutate_status_after_first = True
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_wildcard_rejects_actor_drift_during_attestation():
    client = WildcardClient()
    client.mutate_actor_after_first = True
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_unsupported_glob_construct_fails_closed():
    client = WildcardClient()
    assert await evidence(client, ("[a-z]regression*",)) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_wildcard_requires_matching_workflow_and_job_name():
    client = WildcardClient()
    client.job["name"] = "unrelated"
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_full_merge_accepts_exact_green_wildcard_in_both_pre_post_gates(monkeypatch):
    client = WildcardClient()
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    outcome = await remote.gitea_merge_pull_request(
        "owner", "repo", 25, HEAD, expected_base_sha="b" * 40
    )
    assert outcome["ok"] is True
    assert outcome["result"]["merged"] is True
    assert len(client.merge_calls) == 1
    assert client.branch_reads >= 2
    assert client.status_calls >= 4


@pytest.mark.asyncio
async def test_full_merge_refuses_new_pending_status_at_second_gate_without_post(monkeypatch):
    client = WildcardClient()
    original_get_branch = client.get_branch

    async def change_status_at_second_gate(owner, repo, branch):
        reply = await original_get_branch(owner, repo, branch)
        if client.branch_reads == 2:
            client.statuses.insert(
                0, status(5, "ci / regression (pull_request)", "pending")
            )
        return reply

    client.get_branch = change_status_at_second_gate
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    outcome = await remote.gitea_merge_pull_request(
        "owner", "repo", 25, HEAD, expected_base_sha="b" * 40
    )
    assert outcome["ok"] is False
    assert outcome["error"]["code"] == "CI_NOT_GREEN"
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_multi_pattern_requires_both_real_green_statuses():
    client = WildcardClient()
    assert await evidence(
        client, ("*regression*", "ci / regress* (pull_request)")
    ) == "CI_GREEN"


@pytest.mark.asyncio
async def test_paged_status_history_must_read_all_pages_before_approval():
    client = WildcardClient()
    client.statuses = [
        status(52, "ci / regression (pull_request)", "success"),
        status(51, "ci / regression (pull_request)", "pending"),
        *[
            status(i, f"ci / historical-{i} (pull_request)", "success")
            for i in range(50, 1, -1)
        ],
    ]
    calls = []
    async def paged(owner, repo, sha, *, page, limit):
        assert (owner, repo, sha, limit) == ("owner", "repo", HEAD, 50)
        calls.append(page)
        offset = (page - 1) * limit
        return deepcopy(client.statuses[offset:offset + limit])

    client.list_commit_statuses = paged
    assert await evidence(client) == "CI_GREEN"
    assert calls == [1, 2, 1, 2]


@pytest.mark.asyncio
async def test_out_of_order_status_ids_never_authorize_wildcard():
    client = WildcardClient()
    client.statuses = [
        status(4, "ci / regression (pull_request)", "success"),
        status(5, "ci / regression (pull_request)", "pending"),
    ]
    assert await evidence(client) == "CI_EVIDENCE_INCOMPLETE"
