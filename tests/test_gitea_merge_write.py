"""Security and contract tests for the narrow Gitea PR merge tool."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_client import (
    GiteaClient,
    GiteaMutationOutcomeUnknown,
)
from examples.mcp_server.mcp_infra.adapters import remote

SHA = "a" * 40
BASE_SHA = "1" * 40
NEW_BASE_SHA = "2" * 40
_DEFAULT_WORKFLOW_PAYLOAD = object()


@pytest.mark.asyncio
async def test_client_merge_pr_uses_fixed_endpoint_and_optimistic_head_lock(monkeypatch):
    client = GiteaClient("token")
    post = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_post", post)
    try:
        result = await client.merge_pull_request(
            "owner",
            "repo",
            25,
            expected_head_sha=SHA.upper(),
        )
    finally:
        await client.aclose()

    assert result == {}
    post.assert_awaited_once_with(
        "/repos/{owner}/{repo}/pulls/{number}/merge",
        {"Do": "merge", "head_commit_id": SHA},
        owner="owner",
        repo="repo",
        number=25,
    )


@pytest.mark.asyncio
async def test_client_merge_pr_sends_squash_method_verbatim(monkeypatch):
    client = GiteaClient("token")
    post = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_post", post)
    try:
        result = await client.merge_pull_request(
            "owner",
            "repo",
            25,
            expected_head_sha=SHA,
            method="squash",
        )
    finally:
        await client.aclose()

    assert result == {}
    post.assert_awaited_once_with(
        "/repos/{owner}/{repo}/pulls/{number}/merge",
        {"Do": "squash", "head_commit_id": SHA},
        owner="owner",
        repo="repo",
        number=25,
    )


@pytest.mark.asyncio
async def test_client_merge_pr_rejects_invalid_sha_and_unsupported_methods(monkeypatch):
    client = GiteaClient("token")
    post = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_post", post)
    try:
        with pytest.raises(ValueError, match="40-character SHA-1"):
            await client.merge_pull_request("owner", "repo", 1, expected_head_sha="abc")
        with pytest.raises(ValueError, match="merge method must be one of"):
            await client.merge_pull_request(
                "owner", "repo", 1, expected_head_sha=SHA, method="rebase"
            )
        with pytest.raises(ValueError, match="pull_number"):
            await client.merge_pull_request("owner", "repo", 0, expected_head_sha=SHA)
    finally:
        await client.aclose()

    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_merge_pr_marks_malformed_success_body_as_ambiguous(monkeypatch):
    client = GiteaClient("token")
    request = httpx.Request("POST", "https://git.example/api/v1/repos/owner/repo/pulls/25/merge")
    response = httpx.Response(200, request=request, content=b"{not-json")
    post = AsyncMock(return_value=response)
    monkeypatch.setattr(client._client, "post", post)
    try:
        with pytest.raises(GiteaMutationOutcomeUnknown, match="undecodable success response"):
            await client.merge_pull_request("owner", "repo", 25, expected_head_sha=SHA)
    finally:
        await client.aclose()

    assert post.await_count == 1


class FakeMergeClient:
    def __init__(
        self,
        token: str,
        *,
        ci_conclusion: str = "success",
        head_sha: str = SHA,
        base_sha: str = BASE_SHA,
        latest_head_sha: str | None = None,
        latest_base_sha: str | None = None,
        behind_by: int = 0,
        merge_exception: Exception | None = None,
        apply_merge: bool = True,
        post_merge_read_failures: int = 0,
        workflow_payload: object | None = None,
        second_ci_run: dict[str, object] | None = None,
    ):
        assert token == "token"
        self.ci_conclusion = ci_conclusion
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.latest_head_sha = latest_head_sha
        self.latest_base_sha = latest_base_sha
        self.behind_by = behind_by
        self.merge_exception = merge_exception
        self.apply_merge = apply_merge
        self.post_merge_read_failures = post_merge_read_failures
        self.workflow_payload = (
            workflow_payload
            if workflow_payload is not None
            else {"total_count": 1, "workflows": [{}]}
        )
        self.second_ci_run = second_ci_run
        self.merged = False
        self.compare_calls: list[tuple[str, str]] = []
        self.merge_calls: list[dict] = []
        self.action_run_pages: list[int] = []
        self.operations: list[str] = []
        self.workflow_reads = 0
        self.pr_reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_pull_request(self, owner: str, repo: str, pull_number: int):
        self.pr_reads += 1
        if self.merge_calls and self.post_merge_read_failures > 0:
            self.post_merge_read_failures -= 1
            raise httpx.ReadTimeout("lost post-merge response")
        current_head_sha = (
            self.head_sha if self.pr_reads == 1 else self.latest_head_sha or self.head_sha
        )
        current_base_sha = (
            self.base_sha if self.pr_reads == 1 else self.latest_base_sha or self.base_sha
        )
        return {
            "number": pull_number,
            "state": "closed" if self.merged else "open",
            "merged": self.merged,
            "mergeable": True,
            "merge_commit_sha": "b" * 40 if self.merged else None,
            "head": {"sha": current_head_sha, "ref": "feat/x"},
            "base": {"sha": current_base_sha, "ref": "master"},
            "html_url": "https://git.example/pr/25",
        }

    async def list_workflows(self, owner: str, repo: str):
        self.workflow_reads += 1
        self.operations.append("list_workflows")
        return self.workflow_payload

    async def list_action_runs(
        self,
        owner: str,
        repo: str,
        status: str | None,
        limit: int,
        page: int,
    ):
        assert status is None
        assert limit == 50
        assert page in {1, 2}
        self.action_run_pages.append(page)
        self.operations.append(f"list_action_runs:{page}")
        run = self.second_ci_run if self.workflow_reads >= 2 and self.second_ci_run else None
        if run is None:
            run = {
                "id": 765,
                "event": "pull_request",
                "head_sha": self.head_sha,
                "status": "completed",
                "conclusion": self.ci_conclusion,
            }
        return {"total_count": 1, "workflow_runs": [run]}

    async def compare_commits(self, owner: str, repo: str, *, base: str, head: str):
        self.compare_calls.append((base, head))
        current_head_sha = self.latest_head_sha or self.head_sha
        current_base_sha = self.latest_base_sha or self.base_sha
        if base == current_base_sha and head == current_head_sha:
            return {"total_commits": 1, "commits": [{"sha": current_head_sha}]}
        if base == current_head_sha and head == current_base_sha:
            return {"total_commits": self.behind_by, "commits": [{}] * self.behind_by}
        if base == self.base_sha and head == self.head_sha:
            return {"total_commits": 1, "commits": [{"sha": self.head_sha}]}
        if base == self.head_sha and head == self.base_sha:
            return {"total_commits": self.behind_by, "commits": [{}] * self.behind_by}
        raise AssertionError(f"unexpected compare: {base!r}...{head!r}")

    async def merge_pull_request(self, owner: str, repo: str, pull_number: int, **kwargs):
        self.operations.append("merge")
        self.merge_calls.append(
            {"owner": owner, "repo": repo, "pull_number": pull_number, **kwargs}
        )
        if self.apply_merge:
            self.merged = True
        if self.merge_exception is not None:
            raise self.merge_exception
        return {}


def _ci_run(
    run_id: int,
    *,
    head_sha: str = SHA,
    event: str = "pull_request",
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, object]:
    return {
        "id": run_id,
        "event": event,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
    }


def _ci_page(runs: list[object], total_count: int | None = None) -> dict[str, object]:
    return {
        "total_count": len(runs) if total_count is None else total_count,
        "workflow_runs": runs,
    }


def _ci_descending_page(
    start_id: int,
    count: int,
    *,
    total_count: int,
    head_sha: str = "c" * 40,
    event: str = "pull_request",
    conclusion: str | None = "success",
) -> dict[str, object]:
    return _ci_page(
        [
            _ci_run(
                start_id - index,
                head_sha=head_sha,
                event=event,
                conclusion=conclusion,
            )
            for index in range(count)
        ],
        total_count=total_count,
    )


async def _ci_evidence_status(
    action_pages: list[object],
    workflow_payload: object = _DEFAULT_WORKFLOW_PAYLOAD,
) -> tuple[str, AsyncMock]:
    client = AsyncMock()
    client.list_workflows.return_value = (
        {"total_count": 1, "workflows": [{}]}
        if workflow_payload is _DEFAULT_WORKFLOW_PAYLOAD
        else workflow_payload
    )
    client.list_action_runs.side_effect = action_pages
    status = await remote._gitea_ci_evidence(client, "owner", "repo", SHA)
    return status, client


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"total_count": 0, "workflows": []}, 0),
        ({"total_count": 3, "workflows": []}, 3),
        ({"total_count": 3, "workflows": [{}, {}]}, 3),
        ({"total_count": 2}, 2),
        ({"workflows": [{}]}, 1),
        ({}, None),
        ({"total_count": 0, "workflows": [{}]}, None),
        ({"total_count": 1, "workflows": [{}, {}]}, None),
        ({"total_count": True, "workflows": []}, None),
        ({"total_count": "1", "workflows": []}, None),
        ({"total_count": -1, "workflows": []}, None),
        ({"workflows": {}}, None),
        (None, None),
    ],
)
def test_configured_workflow_count_requires_consistent_signals(payload, expected):
    assert remote._configured_workflow_count(payload) == expected


@pytest.mark.asyncio
async def test_ci_evidence_distinguishes_no_workflows_from_malformed_inventory():
    status, client = await _ci_evidence_status(
        [],
        workflow_payload={"total_count": 0, "workflows": []},
    )
    assert status == "CI_NOT_CONFIGURED"
    client.list_action_runs.assert_not_awaited()

    for payload in (
        None,
        {},
        {"total_count": 0, "workflows": [{}]},
        {"total_count": "1", "workflows": []},
        {"workflows": {}},
    ):
        status, client = await _ci_evidence_status([], workflow_payload=payload)
        assert status == "CI_EVIDENCE_INCOMPLETE"
        client.list_action_runs.assert_not_awaited()


@pytest.mark.asyncio
async def test_ci_evidence_distinguishes_no_exact_head_from_incompatible_trigger():
    stale_page = _ci_page([_ci_run(1, head_sha="c" * 40)])
    status, _ = await _ci_evidence_status([stale_page, stale_page])
    assert status == "NO_REQUIRED_RUN_FOUND"

    push_page = _ci_page([_ci_run(2, event="push")])
    status, _ = await _ci_evidence_status([push_page, push_page])
    assert status == "CI_TRIGGER_INCOMPATIBLE"


@pytest.mark.asyncio
async def test_ci_evidence_finds_exact_head_pull_request_run_on_later_page():
    total_count = remote._GITEA_CI_MAX_RUNS + 431
    first_page = _ci_descending_page(100, 50, total_count=total_count)
    second_page = _ci_descending_page(50, 50, total_count=total_count, head_sha=SHA)

    status, client = await _ci_evidence_status([first_page, second_page, first_page])

    assert status == "CI_GREEN"
    assert [call.kwargs["page"] for call in client.list_action_runs.await_args_list] == [1, 2, 1]
    assert all(call.kwargs["status"] is None for call in client.list_action_runs.await_args_list)
    assert all(call.kwargs["limit"] == 50 for call in client.list_action_runs.await_args_list)


@pytest.mark.asyncio
async def test_ci_evidence_proves_green_head_on_first_page_beyond_max_runs():
    total_count = remote._GITEA_CI_MAX_RUNS + 431
    newest_run = _ci_run(1431, head_sha=SHA)
    older_runs = [_ci_run(1430 - index) for index in range(remote._GITEA_CI_PAGE_LIMIT - 1)]
    first_page = _ci_page([newest_run, *older_runs], total_count=total_count)

    status, client = await _ci_evidence_status([first_page, first_page])

    assert status == "CI_GREEN"
    assert [call.kwargs["page"] for call in client.list_action_runs.await_args_list] == [1, 1]
    assert client.list_action_runs.await_count == 2


@pytest.mark.asyncio
async def test_ci_evidence_fails_closed_when_bounded_prefix_exhausts_without_qualifying_run():
    total_count = remote._GITEA_CI_MAX_RUNS + 431
    start_id = total_count
    pages = [
        _ci_descending_page(
            start_id - page_index * remote._GITEA_CI_PAGE_LIMIT,
            remote._GITEA_CI_PAGE_LIMIT,
            total_count=total_count,
        )
        for page_index in range(remote._GITEA_CI_MAX_PAGES)
    ]

    status, client = await _ci_evidence_status(pages)

    assert status == "CI_EVIDENCE_INCOMPLETE"
    assert client.list_action_runs.await_count == remote._GITEA_CI_MAX_PAGES
    assert [call.kwargs["page"] for call in client.list_action_runs.await_args_list] == list(
        range(1, remote._GITEA_CI_MAX_PAGES + 1)
    )
    assert remote._GITEA_CI_MAX_RUNS < total_count


@pytest.mark.asyncio
async def test_ci_evidence_fails_closed_on_run_bound_with_unscanned_history(monkeypatch):
    total_count = remote._GITEA_CI_MAX_RUNS + 431
    fetched_page_budget = remote._GITEA_CI_MAX_PAGES
    monkeypatch.setattr(remote, "_GITEA_CI_MAX_PAGES", 10_000)
    pages = [
        _ci_descending_page(
            total_count - page_index * remote._GITEA_CI_PAGE_LIMIT,
            remote._GITEA_CI_PAGE_LIMIT,
            total_count=total_count,
        )
        for page_index in range(fetched_page_budget)
    ]

    status, client = await _ci_evidence_status(pages)

    assert status == "CI_EVIDENCE_INCOMPLETE"
    assert client.list_action_runs.await_count == fetched_page_budget
    assert [call.kwargs["page"] for call in client.list_action_runs.await_args_list] == list(
        range(1, fetched_page_budget + 1)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_runs",
    [
        [_ci_run(1), _ci_run(2)],
        [_ci_run(2), _ci_run(1), _ci_run(3)],
    ],
    ids=["ascending", "reshuffled"],
)
async def test_ci_evidence_rejects_non_descending_run_ids_within_page(first_runs):
    page = _ci_page(first_runs, total_count=len(first_runs))

    status, client = await _ci_evidence_status([page, page])

    assert status == "CI_EVIDENCE_INCOMPLETE"
    assert client.list_action_runs.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("second_page_id", [101, 150])
async def test_ci_evidence_rejects_non_descending_run_ids_across_pages(second_page_id):
    first_page = _ci_descending_page(100, 50, total_count=51)
    second_page = _ci_descending_page(
        second_page_id,
        1,
        total_count=51,
        head_sha=SHA,
    )

    status, client = await _ci_evidence_status([first_page, second_page, first_page])

    assert status == "CI_EVIDENCE_INCOMPLETE"
    assert client.list_action_runs.await_count == 2


@pytest.mark.asyncio
async def test_ci_evidence_uses_newest_exact_head_pull_request_run():
    newest_green_first = [_ci_run(11), _ci_run(10, conclusion="failure")]
    green_page = _ci_page(newest_green_first)
    status, _ = await _ci_evidence_status([green_page, green_page])
    assert status == "CI_GREEN"

    newest_failure_first = [_ci_run(11, conclusion="failure"), _ci_run(10)]
    failed_page = _ci_page(newest_failure_first)
    status, _ = await _ci_evidence_status([failed_page, failed_page])
    assert status == "CI_NOT_GREEN"


@pytest.mark.asyncio
async def test_ci_evidence_uses_newest_pull_request_run_over_older_push_run():
    runs = [
        _ci_run(100),
        _ci_run(99, event="push", conclusion="failure"),
        _ci_run(98),
        *[_ci_run(97 - index) for index in range(remote._GITEA_CI_PAGE_LIMIT - 3)],
    ]
    page = _ci_page(runs, total_count=remote._GITEA_CI_MAX_RUNS + 431)

    status, client = await _ci_evidence_status([page, page])

    assert status == "CI_GREEN"
    assert client.list_action_runs.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [True, "7", 7.5, None])
async def test_ci_evidence_rejects_bool_string_and_other_run_ids(bad_id):
    page = {"total_count": 1, "workflow_runs": [{"id": bad_id}]}
    status, client = await _ci_evidence_status([page])
    assert status == "CI_EVIDENCE_INCOMPLETE"
    client.list_action_runs.assert_awaited_once()


@pytest.mark.asyncio
async def test_ci_evidence_rejects_non_object_run():
    page = {"total_count": 1, "workflow_runs": [None]}
    status, _ = await _ci_evidence_status([page])
    assert status == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_ci_evidence_rejects_malformed_run_lists_from_real_client(monkeypatch):
    for raw_page in (
        {"total_count": 0},
        {"total_count": 0, "workflow_runs": None},
        {"total_count": 0, "workflow_runs": {}},
        {"total_count": 0, "workflow_runs": ""},
        [],
    ):
        client = GiteaClient("token")

        async def list_workflows(owner: str, repo: str):
            return {"total_count": 1, "workflows": [{}]}

        get = AsyncMock(return_value=raw_page)
        monkeypatch.setattr(client, "list_workflows", list_workflows)
        monkeypatch.setattr(client, "_get", get)
        try:
            status = await remote._gitea_ci_evidence(client, "owner", "repo", SHA)
        finally:
            await client.aclose()
        assert status == "CI_EVIDENCE_INCOMPLETE"
        get.assert_awaited_once()


@pytest.mark.asyncio
async def test_ci_evidence_rejects_duplicate_ids_within_and_across_pages():
    duplicate_page = _ci_page([_ci_run(1), _ci_run(1)])
    status, _ = await _ci_evidence_status([duplicate_page])
    assert status == "CI_EVIDENCE_INCOMPLETE"

    first_page = _ci_descending_page(100, 50, total_count=51)
    second_page = _ci_descending_page(51, 1, total_count=51, head_sha=SHA)
    status, client = await _ci_evidence_status([first_page, second_page])
    assert status == "CI_EVIDENCE_INCOMPLETE"
    assert client.list_action_runs.await_count == 2


@pytest.mark.asyncio
async def test_ci_evidence_rejects_changed_total_count_across_pages():
    first_page = _ci_descending_page(100, 50, total_count=51)
    second_page = _ci_descending_page(50, 1, total_count=52, head_sha=SHA)

    status, client = await _ci_evidence_status([first_page, second_page])

    assert status == "CI_EVIDENCE_INCOMPLETE"
    assert client.list_action_runs.await_count == 2


@pytest.mark.asyncio
async def test_ci_evidence_rejects_early_empty_or_short_page():
    empty_first = _ci_page([], total_count=1)
    status, _ = await _ci_evidence_status([empty_first])
    assert status == "CI_EVIDENCE_INCOMPLETE"

    first_page = _ci_descending_page(101, 50, total_count=101)
    short_second_page = _ci_descending_page(51, 49, total_count=101, head_sha=SHA)
    status, client = await _ci_evidence_status([first_page, short_second_page, first_page])
    assert status == "CI_EVIDENCE_INCOMPLETE"
    assert client.list_action_runs.await_count == 2


@pytest.mark.asyncio
async def test_ci_evidence_accepts_exact_run_bound_without_requesting_page_21():
    pages = []
    for page_index in range(remote._GITEA_CI_MAX_PAGES):
        first_id = (remote._GITEA_CI_MAX_PAGES - page_index) * remote._GITEA_CI_PAGE_LIMIT
        pages.append(
            _ci_descending_page(
                first_id,
                remote._GITEA_CI_PAGE_LIMIT,
                total_count=remote._GITEA_CI_MAX_RUNS,
                head_sha=SHA if page_index == remote._GITEA_CI_MAX_PAGES - 1 else "c" * 40,
            )
        )

    status, client = await _ci_evidence_status([*pages, pages[0]])

    assert status == "CI_GREEN"
    assert [call.kwargs["page"] for call in client.list_action_runs.await_args_list] == [
        *range(1, remote._GITEA_CI_MAX_PAGES + 1),
        1,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reread",
    [
        {"total_count": 3, "workflow_runs": [_ci_run(2), _ci_run(1)]},
        {"workflow_runs": [_ci_run(2), _ci_run(1)]},
        {"total_count": 2, "workflow_runs": [_ci_run(1), _ci_run(2)]},
        {"total_count": 2, "workflow_runs": [_ci_run(1), _ci_run(1)]},
        {"total_count": 2, "workflow_runs": [_ci_run(2), _ci_run(3)]},
    ],
)
async def test_ci_evidence_rejects_page_one_reread_drift(reread):
    initial_page = _ci_page([_ci_run(2), _ci_run(1)])
    status, _ = await _ci_evidence_status([initial_page, reread])
    assert status == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        ("CI_NOT_CONFIGURED", False),
        ("NO_REQUIRED_RUN_FOUND", False),
        ("CI_TRIGGER_INCOMPATIBLE", False),
        ("CI_EVIDENCE_INCOMPLETE", True),
        ("CI_NOT_GREEN", True),
    ],
)
def test_ci_evidence_tool_error_maps_status_and_retryability(status, retryable):
    result = remote._ci_evidence_tool_error(status)
    assert result["error"]["code"] == status
    assert result["error"]["retryable"] is retryable
    assert result["meta"]["source"] == "gitea"
    assert "token" not in repr(result).lower()


@pytest.mark.asyncio
async def test_adapter_merges_only_expected_green_head_and_confirms_result(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is True
    assert client.merge_calls == [
        {
            "owner": "owner",
            "repo": "repo",
            "pull_number": 25,
            "expected_head_sha": SHA,
            "method": "merge",
        }
    ]
    assert result["result"] == {
        "number": 25,
        "merged": True,
        "outcome": "completed",
        "head_sha": SHA,
        "base": "master",
        "base_sha": BASE_SHA,
        "method": "merge",
        "branch_tracking": {
            "base_ref": "master",
            "base_sha": BASE_SHA,
            "head_ref": "feat/x",
            "head_sha": SHA,
            "compare_by": "sha",
            "branch_contains_base": True,
            "branch_is_current": True,
            "ahead_by": 1,
            "behind_by": 0,
            "warning": None,
            "operator_choices": [],
        },
        "outdated_base_accepted": False,
        "merge_commit_sha": "b" * 40,
        "html_url": "https://git.example/pr/25",
        "reconciliation_attempts": 1,
    }
    assert client.compare_calls == [
        (BASE_SHA, SHA),
        (SHA, BASE_SHA),
        (BASE_SHA, SHA),
        (SHA, BASE_SHA),
    ]
    assert client.pr_reads == 3
    assert client.workflow_reads == 2
    assert client.action_run_pages == [1, 1, 1, 1]
    assert client.operations[-2:] == ["list_action_runs:1", "merge"]
    assert "token" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow_payload", "code", "retryable"),
    [
        ({"total_count": 0, "workflows": []}, "CI_NOT_CONFIGURED", False),
        ({"total_count": 0, "workflows": [{}]}, "CI_EVIDENCE_INCOMPLETE", True),
    ],
)
async def test_adapter_fails_closed_on_unprovable_workflow_inventory(
    monkeypatch,
    workflow_payload,
    code,
    retryable,
):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", workflow_payload=workflow_payload)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert result["error"]["retryable"] is retryable
    assert client.action_run_pages == []
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_passes_squash_through_existing_merge_guards(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request(
        "owner",
        "repo",
        25,
        SHA,
        expected_base_sha=BASE_SHA,
        method="squash",
    )

    assert result["ok"] is True
    assert result["result"]["method"] == "squash"
    assert client.merge_calls == [
        {
            "owner": "owner",
            "repo": "repo",
            "pull_number": 25,
            "expected_head_sha": SHA,
            "method": "squash",
        }
    ]


@pytest.mark.asyncio
async def test_adapter_rejects_unsupported_merge_method_before_remote_reads(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA, method="rebase")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert client.pr_reads == 0
    assert client.compare_calls == []
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_changed_head_before_merge(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", head_sha="c" * 40)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_outdated_branch_before_using_ci(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", behind_by=2)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "PR_BRANCH_OUTDATED"
    tracking = result["error"]["details"]["branch_tracking"]
    assert tracking["branch_is_current"] is False
    assert tracking["behind_by"] == 2
    assert tracking["warning"] == "PR_BRANCH_OUTDATED"
    assert tracking["operator_choices"][0]["action"] == "update_branch_to_base_and_rerun_ci"
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_allows_outdated_branch_only_with_explicit_override(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", behind_by=1)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request(
        "owner",
        "repo",
        25,
        SHA,
        allow_outdated_base=True,
    )

    assert result["ok"] is True
    assert result["result"]["outdated_base_accepted"] is True
    assert result["result"]["branch_tracking"]["branch_is_current"] is False
    assert len(client.merge_calls) == 1


@pytest.mark.asyncio
async def test_adapter_rejects_non_green_ci_before_merge(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", ci_conclusion="failure")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "CI_NOT_GREEN"
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_second_ci_proof_blocks_new_pending_run_before_post(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient(
        "token",
        second_ci_run={
            "id": 766,
            "event": "pull_request",
            "head_sha": SHA,
            "status": "in_progress",
            "conclusion": None,
        },
    )
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "CI_NOT_GREEN"
    assert result["error"]["retryable"] is True
    assert client.workflow_reads == 2
    assert client.action_run_pages == [1, 1, 1, 1]
    assert client.operations[-1] == "list_action_runs:1"
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_uses_newest_matching_ci_run(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token")

    async def list_runs(owner: str, repo: str, status: str | None, limit: int, page: int):
        assert status is None
        assert limit == 50
        assert page == 1
        return {
            "total_count": 2,
            "workflow_runs": [
                {
                    "id": 11,
                    "event": "pull_request",
                    "head_sha": SHA,
                    "status": "completed",
                    "conclusion": "failure",
                },
                {
                    "id": 10,
                    "event": "pull_request",
                    "head_sha": SHA,
                    "status": "completed",
                    "conclusion": "success",
                },
            ]
        }

    client.list_action_runs = list_runs  # type: ignore[method-assign]
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "CI_NOT_GREEN"
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_expected_base_sha_mismatch(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request(
        "owner",
        "repo",
        25,
        SHA,
        expected_base_sha=NEW_BASE_SHA,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "BASE_MISMATCH"
    assert result["error"]["details"] == {
        "expected_base_sha": NEW_BASE_SHA,
        "observed_base_sha": BASE_SHA,
    }
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_base_advancing_after_green_ci(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", latest_base_sha=NEW_BASE_SHA)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "BASE_MISMATCH"
    assert result["error"]["details"] == {
        "expected_base_sha": BASE_SHA,
        "observed_base_sha": NEW_BASE_SHA,
    }
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_reconciles_transport_lost_after_server_applied_merge(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    timeout = httpx.ReadTimeout(
        "merge response lost",
        request=httpx.Request("POST", "https://git.example/api/merge"),
    )
    client = FakeMergeClient("token", merge_exception=timeout, apply_merge=True)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is True
    assert result["result"]["outcome"] == "completed_after_ambiguous_response"
    assert result["result"]["merge_commit_sha"] == "b" * 40
    assert result["result"]["reconciliation_attempts"] == 1
    assert len(client.merge_calls) == 1


@pytest.mark.asyncio
async def test_adapter_reconciles_http_500_after_server_applied_merge(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    request = httpx.Request("POST", "https://git.example/api/merge")
    response = httpx.Response(500, request=request)
    server_error = httpx.HTTPStatusError(
        "server response lost after write",
        request=request,
        response=response,
    )
    client = FakeMergeClient("token", merge_exception=server_error, apply_merge=True)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is True
    assert result["result"]["outcome"] == "completed_after_ambiguous_response"
    assert result["result"]["merge_commit_sha"] == "b" * 40
    assert len(client.merge_calls) == 1


@pytest.mark.asyncio
async def test_adapter_reconciles_malformed_2xx_body_after_server_applied_merge(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient(
        "token",
        merge_exception=GiteaMutationOutcomeUnknown("undecodable success response"),
        apply_merge=True,
    )
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is True
    assert result["result"]["outcome"] == "completed_after_ambiguous_response"
    assert result["result"]["merge_commit_sha"] == "b" * 40
    assert len(client.merge_calls) == 1


@pytest.mark.asyncio
async def test_adapter_unresolved_lost_merge_response_forbids_blind_retry(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    timeout = httpx.ReadTimeout(
        "merge response lost",
        request=httpx.Request("POST", "https://git.example/api/merge"),
    )
    client = FakeMergeClient("token", merge_exception=timeout, apply_merge=False)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert "Do not retry" in result["error"]["hint"]
    details = result["error"]["details"]
    assert details["mutation_started"] is True
    assert details["mutation_error_class"] == "ReadTimeout"
    assert details["reconciliation_attempts"] == 3
    assert details["observed"]["merged"] is False
    assert len(client.merge_calls) == 1


@pytest.mark.asyncio
async def test_adapter_cancellation_after_merge_boundary_reconciles_without_replay(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    started = asyncio.Event()

    class CancelAfterApplyClient(FakeMergeClient):
        async def merge_pull_request(self, owner: str, repo: str, pull_number: int, **kwargs):
            self.merge_calls.append(
                {"owner": owner, "repo": repo, "pull_number": pull_number, **kwargs}
            )
            self.merged = True
            started.set()
            await asyncio.sleep(3600)

    client = CancelAfterApplyClient("token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    task = asyncio.create_task(remote.gitea_merge_pull_request("owner", "repo", 25, SHA))
    await started.wait()
    task.cancel()
    result = await task

    assert result["ok"] is True
    assert result["result"]["outcome"] == "completed_after_ambiguous_response"
    assert result["result"]["merge_commit_sha"] == "b" * 40
    assert len(client.merge_calls) == 1
    assert task.cancelled() is False
    assert task.cancelling() == 0


@pytest.mark.asyncio
async def test_adapter_success_response_without_provable_postcondition_is_unknown(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", apply_merge=True, post_merge_read_failures=3)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["reconciliation_attempts"] == 3
    assert result["error"]["details"]["last_read_error_class"] == "ReadTimeout"
    assert len(client.merge_calls) == 1


@pytest.mark.asyncio
async def test_adapter_postcondition_read_retries_without_replaying_mutation(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", apply_merge=True, post_merge_read_failures=2)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is True
    assert result["result"]["outcome"] == "completed"
    assert result["result"]["reconciliation_attempts"] == 3
    assert len(client.merge_calls) == 1


@pytest.mark.asyncio
async def test_reconciliation_requires_merge_commit_sha_even_when_merged_true():
    client = AsyncMock()
    client.get_pull_request.return_value = {
        "state": "closed",
        "merged": True,
        "merge_commit_sha": None,
        "head": {"sha": SHA, "ref": "feat/x"},
        "base": {"sha": BASE_SHA, "ref": "master"},
    }

    merged_pr, details = await remote._reconcile_merge_postcondition(
        client,
        "owner",
        "repo",
        25,
        expected_head_sha=SHA,
    )

    assert merged_pr is None
    assert details["reconciliation_attempts"] == 3
    assert details["observed"]["merged"] is True
    assert details["observed"]["merge_commit_sha"] is None
    assert client.get_pull_request.await_count == 3


@pytest.mark.asyncio
async def test_reconciliation_rejects_merged_state_for_different_head():
    other_head = "c" * 40
    client = AsyncMock()
    client.get_pull_request.return_value = {
        "state": "closed",
        "merged": True,
        "merge_commit_sha": "b" * 40,
        "head": {"sha": other_head, "ref": "feat/x"},
        "base": {"sha": BASE_SHA, "ref": "master"},
    }

    merged_pr, details = await remote._reconcile_merge_postcondition(
        client,
        "owner",
        "repo",
        25,
        expected_head_sha=SHA,
    )

    assert merged_pr is None
    assert details["reconciliation_attempts"] == 3
    assert details["observed"]["head_sha"] == other_head
    assert details["observed"]["merge_commit_sha"] == "b" * 40
    assert client.get_pull_request.await_count == 3


@pytest.mark.asyncio
async def test_get_pull_request_exposes_merge_provenance_first_class(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token")
    client.merged = True
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_get_pull_request("owner", "repo", 25)

    assert result["ok"] is True
    assert result["result"]["merged"] is True
    assert result["result"]["merge_commit_sha"] == "b" * 40
    assert result["result"]["mergeable"] is True


@pytest.mark.asyncio
async def test_adapter_requires_gitea_token(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPENDENCY_MISSING"
