"""Py Coordination v1: persistent threads, fail-closed mutation and MCP modes."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from examples.mcp_server import coordination as core
from examples.mcp_server.mcp_infra.adapters import coordination_adapter as adapter
from examples.mcp_server.tool_modes import should_register_tool
from examples.mcp_server.tool_scopes import get_required_scopes


class FakeClient:
    def __init__(self) -> None:
        self.issues: list[dict] = []
        self.comments: dict[int, list[dict]] = {}
        self.create_calls = 0
        self.comment_calls = 0
        self.fail_issue_after_write = False
        self.fail_comment_after_write = False

    async def get_user(self):
        return {"login": "gpakoh"}

    async def list_issues(self, owner, repo, *, state="all", limit=50, page=1):
        assert (owner, repo) == ("gpakoh", "agent-ssh-gateway")
        assert state == "all"
        return self.issues[(page - 1) * limit:page * limit]

    async def get_issue(self, owner, repo, number):
        return next(i for i in self.issues if i["number"] == number)

    async def list_issue_comments(self, owner, repo, number, *, limit=50, page=1):
        rows = self.comments.get(number, [])
        return rows[(page - 1) * limit:page * limit]

    async def create_coord_issue(self, owner, repo, *, title, body):
        self.create_calls += 1
        issue = {
            "number": len(self.issues) + 1,
            "title": title,
            "body": body,
            "state": "open",
            "html_url": f"https://gitea.example/gpakoh/agent-ssh-gateway/issues/{len(self.issues)+1}",
        }
        self.issues.append(issue)
        if self.fail_issue_after_write:
            raise OSError("connection lost after POST")
        return issue

    async def create_coord_comment(self, owner, repo, number, *, body):
        self.comment_calls += 1
        comment = {"id": self.comment_calls, "body": body, "user": {"login": "gpakoh"}}
        self.comments.setdefault(number, []).append(comment)
        if self.fail_comment_after_write:
            raise OSError("connection lost after POST")
        return comment


@pytest.mark.asyncio
async def test_create_read_replay_and_comment_state_across_restart():
    a = FakeClient()
    first = await core.create_thread(
        a, "gpakoh", "agent-ssh-gateway", source_project="business-analytics-service",
        title="candidate cleanup blocks", body="WORKSPACE_CONTENDED", request_key="business-ack-20261022",
    )
    again = await core.create_thread(
        a, "gpakoh", "agent-ssh-gateway", source_project="business-analytics-service",
        title="candidate cleanup blocks", body="WORKSPACE_CONTENDED", request_key="business-ack-20261022",
    )
    assert first["created"] and not again["created"] and a.create_calls == 1
    ack = await core.write_event(
        a, "gpakoh", "agent-ssh-gateway", 1,
        text="Accepted by Gateway", request_key="gateway-accept-20261022", state="accepted",
    )
    replay = await core.write_event(
        a, "gpakoh", "agent-ssh-gateway", 1,
        text="Accepted by Gateway", request_key="gateway-accept-20261022", state="accepted",
    )
    assert ack["created"] and not replay["created"] and a.comment_calls == 1
    thread = await core.open_thread(a, "gpakoh", "agent-ssh-gateway", 1)
    assert thread["source_project"] == "business-analytics-service"
    assert thread["coord_state"] == "accepted"
    assert thread["history"][0]["body"] == "Accepted by Gateway"


@pytest.mark.asyncio
async def test_reconcile_ambiguous_posts_without_resending():
    a = FakeClient()
    a.fail_issue_after_write = True
    made = await core.create_thread(
        a, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
        title="Broken", body="Please check", request_key="reconciled-thread",
    )
    assert made["reconciled"] and a.create_calls == 1
    a.fail_comment_after_write = True
    event = await core.write_event(
        a, "gpakoh", "agent-ssh-gateway", 1,
        text="Investigating", request_key="reconciled-event", state="accepted",
    )
    assert event["reconciled"] and a.comment_calls == 1


@pytest.mark.asyncio
async def test_ambiguous_unproven_post_does_not_blindly_retry():
    a = FakeClient()

    async def fail(*args, **kwargs):
        raise OSError("connection lost before result")

    a.create_coord_issue = fail
    with pytest.raises(core.CoordinationOutcomeUnknown, match="do not replay"):
        await core.create_thread(
            a, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
            title="Uncertain", body="Check me", request_key="uncertain-post",
        )


@pytest.mark.asyncio
async def test_inbox_ignores_pull_requests_and_foreign_issues():
    a = FakeClient()
    a.issues.append({
        "number": 1, "title": "PR", "body": core._header(
            kind="issue", key="test-issue-123", source="agent-ssh-gateway", state="open"
        ), "pull_request": {"url": "https://gitea.example/pull/1"},
    })
    a.issues.append({"number": 2, "title": "ordinary issue", "body": "Just a ticket"})
    found = await core.list_threads(a, "gpakoh", "agent-ssh-gateway")
    assert found == []
    with pytest.raises(ValueError, match="not a Py Coordination"):
        await core.open_thread(a, "gpakoh", "agent-ssh-gateway", 2)


@pytest.mark.asyncio
async def test_request_key_conflict_must_not_append_different_event():
    a = FakeClient()
    await core.create_thread(
        a, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
        title="Issue", body="Body", request_key="unique-issue-key",
    )
    await core.write_event(a, "gpakoh", "agent-ssh-gateway", 1,
                           text="A", request_key="event-key-001", state="accepted")
    with pytest.raises(ValueError, match="reused"):
        await core.write_event(a, "gpakoh", "agent-ssh-gateway", 1,
                               text="B", request_key="event-key-001", state="accepted")
    assert a.comment_calls == 1


@pytest.mark.parametrize("project", ["../x", "foo/bar", "x" * 120, ""])
def test_project_validation_rejects_injection(project):
    with pytest.raises(ValueError):
        core.validate_project(project)


@pytest.mark.parametrize("key", ["x", "../escape", "UPPERCASE", "x\ncomment", ""])
def test_key_validation_rejects_invalid(key):
    with pytest.raises(ValueError):
        core.validate_key(key)


@pytest.mark.asyncio
async def test_same_create_key_with_different_body_is_rejected():
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="business-analytics-service",
        title="Cleanup", body="First", request_key="stable-create-key",
    )
    with pytest.raises(ValueError, match="different issue content"):
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway",
            source_project="business-analytics-service",
            title="Cleanup", body="Changed", request_key="stable-create-key",
        )
    assert client.create_calls == 1


@pytest.mark.asyncio
async def test_state_machine_requires_acceptance_and_verification():
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="business-analytics-service",
        title="Cleanup", body="Blocked", request_key="workflow-create-key",
    )
    with pytest.raises(ValueError, match="state transition"):
        await core.write_event(
            client, "gpakoh", "agent-ssh-gateway", 1,
            text="Claimed fixed", request_key="early-resolved-key", state="resolved",
        )
    await core.write_event(
        client, "gpakoh", "agent-ssh-gateway", 1,
        text="Discussion", request_key="neutral-message-key", state="message",
    )
    assert (await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1))["coord_state"] == "open"
    for key, text, state in (
        ("accept-first-key", "I will inspect", "accepted"),
        ("waiting-review-key", "PR green, please verify", "waiting_review"),
        ("business-confirm-key", "Cleanup now succeeded", "resolved"),
    ):
        await core.write_event(
            client, "gpakoh", "agent-ssh-gateway", 1,
            text=text, request_key=key, state=state,
        )
    assert (await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1))["coord_state"] == "resolved"
    with pytest.raises(ValueError, match="state transition"):
        await core.write_event(
            client, "gpakoh", "agent-ssh-gateway", 1,
            text="Late reply", request_key="late-reply-key", state="message",
        )


@pytest.mark.asyncio
async def test_lowlevel_client_separates_coordination_from_generic_pr_post(monkeypatch):
    from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

    client = GiteaClient("fake-token")
    post = AsyncMock(return_value={"number": 10, "body": "body"})
    monkeypatch.setattr(client, "_post_coord", post)
    try:
        result = await client.create_coord_issue(
            "gpakoh", "agent-ssh-gateway", title="Issue", body="body",
        )
        assert result["number"] == 10
        post.assert_awaited_once_with(
            "/repos/{owner}/{repo}/issues", {"title": "Issue", "body": "body"},
            owner="gpakoh", repo="agent-ssh-gateway",
        )
        with pytest.raises(ValueError, match="Write endpoint not allowed"):
            await client._post(
                "/repos/{owner}/{repo}/issues",
                {"title": "not allowed"},
                owner="gpakoh", repo="agent-ssh-gateway",
            )
        with pytest.raises(ValueError, match="Coordination write endpoint not allowed"):
            await GiteaClient._post_coord(client, "/repos/{owner}/{repo}/pulls", {"title": "bad"}, owner="gpakoh", repo="agent-ssh-gateway")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_lowlevel_client_issue_pagination_is_exact_and_validated(monkeypatch):
    from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

    client = GiteaClient("fake-token")
    get = AsyncMock(return_value=[])
    monkeypatch.setattr(client, "_get", get)
    try:
        await client.list_issues("gpakoh", "agent-ssh-gateway", state="all", limit=50, page=3)
        get.assert_awaited_once_with(
            "/repos/{owner}/{repo}/issues",
            params={"state": "all", "limit": 50, "page": 3},
            owner="gpakoh", repo="agent-ssh-gateway",
        )
        with pytest.raises(ValueError, match="positive integer"):
            await client.list_issues("gpakoh", "agent-ssh-gateway", page=True)
        with pytest.raises(ValueError, match="limit"):
            await client.list_issues("gpakoh", "agent-ssh-gateway", limit=51)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_untrusted_gitea_comment_cannot_fake_coordination_resolution():
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="business-analytics-service", title="Safety check",
        body="Please investigate", request_key="spoof-check-issue",
    )
    client.comments.setdefault(1, []).append({
        "id": 101,
        "body": core._header(kind="event", key="spoof-check-comment", state="resolved") + "\\nI am done",
        "user": {"login": "unknown-user"},
    })
    detail = await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)
    assert detail["coord_state"] == "open"
    assert detail["history"][0]["state"] is None


@pytest.mark.asyncio
async def test_adapter_cross_project_round_trip_uses_same_gitea_thread(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_principal", lambda: "0123456789abcdef")
    monkeypatch.setattr(adapter, "_registered", lambda x: core.validate_project(x))
    client = FakeClient()

    async def get_repo(owner, repo):
        assert owner == "gpakoh" and repo == "agent-ssh-gateway"
        return {"permissions": {"push": True}}

    client.get_repo = get_repo
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda key: (lambda _: client) if key == "GiteaClient" else None,
    )

    created = await adapter.coord_create(
        "business-analytics-service", "agent-ssh-gateway",
        "Blocked cleanup", "Evidence: CI green", "business-cleanup-20261022",
    )
    assert created["ok"] is True
    assert created["result"]["thread"]["number"] == 1
    accepted = await adapter.coord_accept(
        "agent-ssh-gateway", 1, "Investigating", "gateway-accept-case-1",
    )
    assert accepted["ok"] is True
    inbox = await adapter.coord_inbox("agent-ssh-gateway")
    assert inbox["ok"] is True
    assert inbox["result"]["count"] == 1
    assert inbox["result"]["threads"][0]["coord_state"] == "accepted"
    opened = await adapter.coord_open("agent-ssh-gateway", 1)
    assert opened["result"]["thread"]["history"][0]["caller"] == "0123456789abcdef"


async def _fake_aenter(client):
    return client


async def _fake_aexit(client, *args):
    return None


@pytest.mark.asyncio
async def test_idempotent_create_and_reply_survive_new_gateway_session():
    client = FakeClient()
    original = "0123456789abcdef"
    rotated = "abcdef0123456789"
    created = await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="business-analytics-service",
        title="Durable handoff", body="Same problem",
        request_key="migrate-chat-issue-1", actor=original,
    )
    replay = await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="business-analytics-service",
        title="Durable handoff", body="Same problem",
        request_key="migrate-chat-issue-1", actor=rotated,
    )
    assert created["created"] is True and replay["created"] is False
    assert replay["thread"]["created_by"] == original
    assert client.create_calls == 1

    first = await core.write_event(
        client, "gpakoh", "agent-ssh-gateway", 1,
        text="Accepted; please review", request_key="migrate-chat-event-1",
        state="accepted", actor=original,
    )
    again = await core.write_event(
        client, "gpakoh", "agent-ssh-gateway", 1,
        text="Accepted; please review", request_key="migrate-chat-event-1",
        state="accepted", actor=rotated,
    )
    assert first["created"] is True and again["created"] is False
    assert again["event"]["caller"] == original
    assert client.comment_calls == 1


@pytest.mark.asyncio
async def test_ambiguous_create_wrong_payload_same_key_is_not_misidentified():
    client = FakeClient()

    async def wrong_write(owner, repo, *, title, body):
        client.issues.append({
            "number": 1, "title": title, "body": body.split("\n", 1)[0] + "\nwrong content",
        })
        raise OSError("response lost")

    client.create_coord_issue = wrong_write
    with pytest.raises(core.CoordinationOutcomeUnknown, match="uncertain"):
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway",
            source_project="business-analytics-service",
            title="Same title", body="expected content",
            request_key="ambiguous-content-mismatch",
        )


@pytest.mark.asyncio
async def test_ambiguous_comment_wrong_state_same_key_is_not_misidentified():
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="business-analytics-service",
        title="Same thread", body="description",
        request_key="ambiguous-comment-issue",
    )

    async def wrong_write(owner, repo, number, *, body):
        client.comments.setdefault(number, []).append({
            "id": 1,
            "body": body.replace("state=accepted", "state=waiting_review"),
            "user": {"login": "gpakoh"},
        })
        raise OSError("response lost")

    client.create_coord_comment = wrong_write
    with pytest.raises(core.CoordinationOutcomeUnknown, match="uncertain"):
        await core.write_event(
            client, "gpakoh", "agent-ssh-gateway", 1,
            text="accepted the case", request_key="ambiguous-comment-replay",
            state="accepted",
        )


@pytest.mark.asyncio
async def test_project_board_projects_gitea_threads_into_four_columns(monkeypatch):
    from examples.mcp_server.mcp_infra.adapters import coordination_adapter as adapter

    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda x: core.validate_project(x))
    fake = FakeClient()

    async def aenter(self):
        return self

    async def aexit(self, *args):
        return None

    monkeypatch.setattr(FakeClient, "__aenter__", aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", aexit, raising=False)
    monkeypatch.setattr(adapter, "server_attr", lambda name: (lambda _: fake) if name == "GiteaClient" else None)
    for state, key in (
        ("open", "board-task-open-123"),
        ("accepted", "board-task-accept-123"),
        ("waiting_review", "board-task-review-123"),
        ("resolved", "board-task-resolved-123"),
    ):
        created = await core.create_thread(
            fake, "gpakoh", "agent-ssh-gateway",
            source_project="business-analytics-service",
            title="Board " + state,
            body="Check state",
            request_key=key,
        )
        n = created["thread"]["number"]
        if state != "open":
            await core.write_event(
                fake, "gpakoh", "agent-ssh-gateway", n,
                text="Acknowledged", request_key=f"board-accept-{n}-123", state="accepted",
            )
        if state in ("waiting_review", "resolved"):
            await core.write_event(
                fake, "gpakoh", "agent-ssh-gateway", n,
                text="PR and tests", request_key=f"board-review-{n}-123", state="waiting_review",
            )
        if state == "resolved":
            await core.write_event(
                fake, "gpakoh", "agent-ssh-gateway", n,
                text="Requester verified cleanup", request_key=f"board-done-{n}-123", state="resolved",
            )
    fake.issues.append({"number": 99, "title": "ordinary", "body": "not a coordination thread"})
    fake.issues.append({
        "number": 100, "title": "PR", "body": core._header(
            kind="issue", key="board-task-ignore-123",
            source="business-analytics-service", state="open",
        ),
        "pull_request": {"url": "https://gitea.example/pulls/100"},
    })
    result = await adapter.coord_board("agent-ssh-gateway")
    assert result["ok"] is True, result
    data = result["result"]
    assert data["count"] == 4
    assert data["truncated"] is False
    assert [col["state"] for col in data["columns"]] == [
        "open", "accepted", "waiting_review", "resolved",
    ]
    assert [col["count"] for col in data["columns"]] == [1, 1, 1, 1]
    assert [col["cards"][0]["number"] for col in data["columns"]] == [1, 2, 3, 4]
    assert all("history" not in c["cards"][0] for c in data["columns"])
    assert data["columns"][-1]["cards"][0]["gitea_issue_state"] == "open"


def test_board_does_not_silently_map_unknown_coord_state():
    with pytest.raises(ValueError, match="invalid coordination state"):
        core.board_from_threads([{"coord_state": "in_progress_without_receipt"}])


def test_readonly_board_visible_in_safe_mcp_mode():
    assert should_register_tool("coord_board", "mcp_client") is True
    assert get_required_scopes("coord_board") == ["mcp:repo"]


@pytest.mark.asyncio
async def test_board_refuses_invalid_limit_before_gitea_connection():
    for limit in (0, -1, 31, True, "10"):
        response = await adapter.coord_board("agent-ssh-gateway", limit=limit)
        assert response["ok"] is False
        assert response["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_large_repo_read_only_board_returns_bounded_truncated_result(monkeypatch):
    """Hundreds of unrelated Gitea PRs must not blank the entire project board."""
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    client.issues = [
        {"number": i + 1, "title": f"PR {i}", "body": "ordinary issue"}
        for i in range(525)
    ]
    client.issues[0] = {
        "number": 1, "title": "Gateway blocker", "state": "open",
        "html_url": "https://gitea.example/gpakoh/agent-ssh-gateway/issues/1",
        "body": core._header(kind="issue", key="large-repo-open-2026",
                              source="business-analytics-service", state="open") + "\nFailure",
    }

    async def enter(self):
        return self

    async def leave(self, *args):
        return None

    monkeypatch.setattr(FakeClient, "__aenter__", enter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", leave, raising=False)
    monkeypatch.setattr(adapter, "server_attr",
                        lambda name: (lambda _: client) if name == "GiteaClient" else None)
    board = await adapter.coord_board("agent-ssh-gateway")
    inbox = await adapter.coord_inbox("agent-ssh-gateway")
    assert board["ok"] is True, board
    assert board["result"]["count"] == 1
    assert board["result"]["truncated"] is True
    assert board["result"]["columns"][0]["cards"][0]["number"] == 1
    assert inbox["ok"] is True, inbox
    assert inbox["result"]["threads"][0]["number"] == 1
    assert inbox["result"]["truncated"] is True

    # Read-only preview must not authorize a new POST. The exact key is
    # outside the 500-row preview window but inside exhaustive write scope.
    client.issues[524] = {
        "number": 525, "title": "Existing beyond preview", "state": "open",
        "html_url": "https://gitea.example/gpakoh/agent-ssh-gateway/issues/525",
        "body": core._header(kind="issue", key="late-key-not-visible",
                              source="business-analytics-service", state="open") + "\nOriginal",
    }
    replay = await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="business-analytics-service",
        title="Existing beyond preview", body="Original", request_key="late-key-not-visible",
    )
    assert replay["created"] is False
    assert client.create_calls == 0


@pytest.mark.asyncio
async def test_create_fails_closed_when_full_write_history_is_unscannable():
    class EndlessGitea(FakeClient):
        async def list_issues(self, owner, repo, *, state="all", limit=50, page=1):
            return [{"number": (page - 1) * limit + i + 1, "body": "ordinary"}
                    for i in range(limit)]

    client = EndlessGitea()
    with pytest.raises(core.CoordinationOutcomeUnknown, match="5000-row"):
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway",
            source_project="business-analytics-service",
            title="Do not duplicate", body="Uncertain old history",
            request_key="endless-reconciliation-safety",
        )
    assert client.create_calls == 0


def test_coord_tools_mode_and_scope_contract():
    for name in ("coord_inbox", "coord_open"):
        assert should_register_tool(name, "mcp_client") is True
        assert name in ("coord_inbox", "coord_open")
    for name in ("coord_create", "coord_reply", "coord_accept", "coord_update"):
        assert should_register_tool(name, "mcp_client") is False
        assert should_register_tool(name, "mcp_client_write") is True
        assert get_required_scopes(name) == ["mcp:repo"]


@pytest.mark.asyncio
async def test_adapter_refuses_write_without_authenticated_caller(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_principal", lambda: None)
    monkeypatch.setattr(adapter, "_registered", lambda x: x)
    response = await adapter.coord_create(
        "business-analytics-service", "agent-ssh-gateway",
        "Candidate failure", "Please help", "new-case-key",
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "AUTH_ERROR"


@pytest.mark.asyncio
async def test_adapter_refuses_unregistered_target_before_gitea_post(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_principal", lambda: "caller-fingerprint")
    monkeypatch.setattr(adapter, "_registered", lambda x: (_ for _ in ()).throw(ValueError("not registered")))
    response = await adapter.coord_create(
        "business-analytics-service", "not-registered",
        "Candidate failure", "Please help", "new-case-key",
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_adapter_write_denies_missing_repo_permission(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_principal", lambda: "caller-fingerprint")
    monkeypatch.setattr(adapter, "_registered", lambda x: x)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        get_repo = AsyncMock(return_value={"permissions": {"pull": True, "push": False}})
        create_coord_issue = AsyncMock()

    fake = Client()
    monkeypatch.setattr(adapter.server_attr, "__unused__", None, raising=False)
    monkeypatch.setattr(adapter, "server_attr", lambda name: (lambda _: fake) if name == "GiteaClient" else None)
    result = await adapter.coord_create(
        "business-analytics-service", "agent-ssh-gateway", "Task", "Body", "valid-task-key",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "AUTH_ERROR"
    fake.create_coord_issue.assert_not_awaited()
