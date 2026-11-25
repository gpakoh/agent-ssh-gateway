"""Py Coordination v1: persistent threads, fail-closed mutation and MCP modes."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app import coordination_logic as coordination_engine
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

    async def list_issue_timeline(self, owner, repo, number, *, limit=50, page=1):
        rows = self.comments.get(number, [])
        # Fake only stores ordinary issue comments unless a test injects
        # explicit service events into the timeline.
        return [
            {"type": "comment", **row}
            for row in rows[(page - 1) * limit:page * limit]
        ]

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


@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        ("gpakoh/NOD_gateway", "NOD_gateway"),
        (None, "nod-gateway"),
    ],
)
def test_source_repo_mapping_preserves_registered_case(mapping, expected, monkeypatch):
    from app.routers import coordination as browser

    metadata = {"type": "monorepo", "root": "/trusted/NOD_gateway"}
    if mapping is not None:
        metadata["gitea_repo"] = mapping
    assert core.resolve_coordination_repo("nod-gateway", "gpakoh", metadata) == expected

    class Registry:
        def project_info(self, project):
            assert project == "nod-gateway"
            return metadata

    monkeypatch.setattr(browser, "get_registry", lambda: Registry())
    monkeypatch.setenv("COORDINATION_GITEA_OWNER", "gpakoh")
    assert browser._source_project("nod-gateway") == expected


@pytest.mark.parametrize(
    "mapping",
    ["other/NOD_gateway", "gpakoh/nod-gateway/extra", "gpakoh/%2f", "gpakoh/", 3, ""],
)
def test_source_repo_mapping_denies_invalid_or_cross_owner_identity(mapping):
    with pytest.raises(ValueError, match="mapping|owner|project"):
        core.resolve_coordination_repo(
            "nod-gateway", "gpakoh",
            {"type": "monorepo", "root": "/trusted/NOD_gateway", "gitea_repo": mapping},
        )


@pytest.mark.asyncio
async def test_mcp_nod_gateway_coord_uses_pinned_case_not_project_id(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setenv("MCP_COORD_GITEA_OWNER", "gpakoh")

    class Registry:
        def project_info(self, project):
            assert project == "nod-gateway"
            return {
                "type": "monorepo",
                "root": "/trusted/NOD_gateway",
                "gitea_repo": "gpakoh/NOD_gateway",
            }

    class NodClient:
        def __init__(self, token):
            assert token == "fake"
            self.requests = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_issues(self, owner, repo, *, state="all", limit=50, page=1):
            self.requests.append((owner, repo, state, page))
            assert (owner, repo) == ("gpakoh", "NOD_gateway")
            return []

    clients = []

    def server(name):
        if name == "_get_workspace_registry":
            return lambda: Registry()
        if name == "GiteaClient":
            def new_client(token):
                instance = NodClient(token)
                clients.append(instance)
                return instance
            return new_client
        raise AssertionError(name)

    monkeypatch.setattr(adapter, "server_attr", server)
    assert adapter._registered("nod-gateway") == "NOD_gateway"
    assert await adapter._connection("coord_board", "nod-gateway", write=False) == (
        "gpakoh", "NOD_gateway"
    )
    result = await adapter.coord_board("nod-gateway", limit=5)
    assert result["ok"] is True, result
    assert result["result"]["count"] == 0
    assert clients[-1].requests == [("gpakoh", "NOD_gateway", "all", 1)]


@pytest.mark.asyncio
async def test_nod_create_uses_exact_repo_and_keeps_source_project_marker(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setenv("MCP_COORD_GITEA_OWNER", "gpakoh")
    monkeypatch.setattr(adapter, "_principal", lambda: "0123456789abcdef")

    class Registry:
        def project_info(self, project):
            assert project in {"nod-master-standalone", "nod-gateway"}
            if project == "nod-gateway":
                return {
                    "type": "monorepo", "root": "/trusted/NOD_gateway",
                    "gitea_repo": "gpakoh/NOD_gateway",
                }
            return {"type": "source", "root": "/trusted/nod-master-standalone"}

    class NodClient:
        created = []

        def __init__(self, token):
            assert token == "fake"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_repo(self, owner, repo):
            assert (owner, repo) == ("gpakoh", "NOD_gateway")
            return {"full_name": "gpakoh/NOD_gateway", "permissions": {"push": True}}

        async def list_issues(self, owner, repo, *, state="all", limit=50, page=1):
            assert (owner, repo, state) == ("gpakoh", "NOD_gateway", "all")
            return []

        async def create_coord_issue(self, owner, repo, *, title, body):
            assert (owner, repo) == ("gpakoh", "NOD_gateway")
            self.created.append(body)
            return {
                "number": 101, "title": title, "body": body, "state": "open",
                "html_url": "https://git.xloud.ru/gpakoh/NOD_gateway/issues/101",
            }

    def server(name):
        if name == "_get_workspace_registry":
            return lambda: Registry()
        if name == "GiteaClient":
            return NodClient
        raise AssertionError(name)

    monkeypatch.setattr(adapter, "server_attr", server)
    result = await adapter.coord_create(
        "nod-master-standalone", "nod-gateway",
        "NOD outbox", "Evidence", "nod-outbox-routing-20261031",
    )
    assert result["ok"] is True, result
    assert result["result"]["target_project"] == "nod-gateway"
    assert result["result"]["gitea_repo"] == "gpakoh/NOD_gateway"
    assert result["result"]["thread"]["source_project"] == "nod-master-standalone"
    assert "source=nod-master-standalone" in NodClient.created[0]


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
            params={"state": "all", "type": "issues", "limit": 50, "page": 3},
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
        return {"full_name": f"{owner}/{repo}", "permissions": {"push": True}}

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
    assert data["count_exact"] is True
    assert data["examined_thread_count"] == 4
    assert all(col["count_exact"] for col in data["columns"])
    assert [col["state"] for col in data["columns"]] == [
        "open", "accepted", "waiting_review", "resolved",
    ]
    assert [col["count"] for col in data["columns"]] == [1, 1, 1, 1]
    assert [col["cards"][0]["number"] for col in data["columns"]] == [1, 2, 3, 4]
    assert all("history" not in c["cards"][0] for c in data["columns"])
    assert data["columns"][-1]["cards"][0]["gitea_issue_state"] == "open"


@pytest.mark.asyncio
async def test_board_and_inbox_isolate_unreadable_long_thread(monkeypatch):
    """A stuck comment page must not make the whole read-only board fail."""
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    for key in ("large-issue-thread-1", "small-issue-thread-2"):
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway",
            source_project="agent-ssh-gateway",
            title=key, body="Evidence", request_key=key,
        )
    await core.write_event(
        client, "gpakoh", "agent-ssh-gateway", 2,
        text="Acknowledged", state="accepted", request_key="small-issue-accepted",
    )
    # A Gitea pagination anomaly can return the same full first page for
    # later offsets. The write path must remain fail-closed and exhaustive.
    repeated = [
        {"id": i + 1, "type": "comment", "body": "untrusted", "user": {"login": "other"}}
        for i in range(50)
    ]
    list_comments = client.list_issue_timeline

    async def stuck_comments(owner, repo, number, *, limit=50, page=1):
        if number == 1:
            return repeated
        return await list_comments(owner, repo, number, limit=limit, page=page)

    client.list_issue_timeline = stuck_comments
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )
    for reader in (adapter.coord_board, adapter.coord_inbox):
        result = await reader("agent-ssh-gateway")
        assert result["ok"] is True, result
        data = result["result"]
        assert data["truncated"] is True
        assert data["count_exact"] is False
        assert data["examined_thread_count"] == 2
        assert data["incomplete_thread_numbers"] == [1]
        assert data["count"] == 1
        if "columns" in data:
            assert all(col["count_exact"] is False for col in data["columns"])
            assert [c["count"] for c in data["columns"]] == [0, 1, 0, 0]
        else:
            assert data["threads"][0]["number"] == 2
            assert data["threads"][0]["coord_state"] == "accepted"
        # Backfill one requested slot beyond the first unreadable thread.
        # No unverified state is invented and totals remain explicitly partial.
        small = (await reader("agent-ssh-gateway", limit=1))["result"]
        assert small["count"] == 1
        assert small["count_exact"] is False
        assert small["truncated"] is True
        assert small["examined_thread_count"] == 2
        assert small["incomplete_thread_numbers"] == [1]
        if "columns" in small:
            assert small["columns"][1]["cards"][0]["number"] == 2
        else:
            assert small["threads"][0]["number"] == 2
    with pytest.raises(core.CoordinationOutcomeUnknown, match="5000-row"):
        await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)



@pytest.mark.asyncio
async def test_opt_in_summary_counts_every_verified_state_without_changing_page(monkeypatch):
    """Global projection is a separate opt-in scan, not page-local counts."""
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    for i in range(5):
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway",
            source_project="agent-ssh-gateway",
            title=f"Summary {i}", body="Evidence",
            request_key=f"summary-thread-{i}-123",
        )
    for number, states in (
        (2, ("accepted",)),
        (3, ("accepted", "waiting_review")),
        (4, ("accepted", "waiting_review", "resolved")),
    ):
        for n, state in enumerate(states):
            await core.write_event(
                client, "gpakoh", "agent-ssh-gateway", number,
                text=f"Proof {state}", state=state,
                request_key=f"summary-{number}-{n}-transition",
            )
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )
    for reader in (adapter.coord_inbox, adapter.coord_board):
        ordinary = (await reader("agent-ssh-gateway", limit=2))["result"]
        assert ordinary["count"] == 2
        assert ordinary["count_exact"] is False
        assert "summary" not in ordinary
        result = await reader("agent-ssh-gateway", limit=2, include_summary=True)
        assert result["ok"] is True, result
        data = result["result"]
        assert data["count"] == 2
        assert data["next_offset"] == 2
        assert data["count_exact"] is False
        summary = data["summary"]
        assert summary["count_exact"] is True
        assert summary["verified_thread_count"] == 5
        assert summary["examined_thread_count"] == 5
        assert summary["observed_thread_count"] == 5
        assert summary["unexamined_thread_count"] == 0
        assert summary["unverified_thread_numbers"] == []
        assert {key: item["count"] for key, item in summary["state_counts"].items()} == {
            "open": 2, "accepted": 1, "waiting_review": 1, "resolved": 1,
        }
        assert all(item["count_exact"] for item in summary["state_counts"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["healthy", "failed_runs", "wrong_repo", "malformed_pr", "full_page", "wrong_run_repo"])
async def test_board_resume_snapshot_is_bounded_exact_and_optional(monkeypatch, scenario):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
        title="Resume", body="Evidence", request_key="resume-snapshot-thread-123",
    )
    repo_name = "gpakoh/agent-ssh-gateway"
    master_sha = "a" * 40
    feature_sha = "b" * 40
    client.get_repo = AsyncMock(return_value={
        "full_name": "other/repo" if scenario == "wrong_repo" else repo_name,
        "default_branch": "master", "open_issues_count": 48,
        "owner": {"email": "secret@example.invalid"},
    })
    client.get_branch = AsyncMock(return_value={
        "name": "master", "commit": {"id": master_sha}, "protected": True,
    })
    pr = {"number": 12, "state": "open", "head": {"sha": feature_sha, "ref": "fix/a"},
          "base": {"sha": master_sha, "ref": "master"}, "body": "PRIVATE BODY"}
    prs = [{**pr, "number": i + 12} for i in range(10)] if scenario == "full_page" else [pr]
    if scenario == "malformed_pr":
        prs.append({"number": 13, "state": "open", "head": {"sha": "invalid"}})
    client.list_pull_requests = AsyncMock(return_value=prs)
    runs = [
        {"id": 101, "head_sha": master_sha, "head_branch": "master", "event": "push",
         "status": "completed", "conclusion": "success", "run_attempt": 1,
         "repository": {"full_name": repo_name}},
        {"id": 102, "head_sha": feature_sha, "event": "pull_request",
         "status": "in_progress", "conclusion": None, "run_attempt": 1,
         "repository": {"full_name": "other/repo" if scenario == "wrong_run_repo" else repo_name}},
        {"id": 103, "head_sha": "c" * 40, "event": "pull_request",
         "status": "completed", "conclusion": "success", "run_attempt": 1,
         "repository": {"full_name": repo_name}},
    ]
    client.list_action_runs = AsyncMock(return_value={"workflow_runs": runs, "total_count": 500})
    if scenario == "failed_runs":
        client.list_action_runs.side_effect = OSError("SECRET PROVIDER ERROR")
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )
    ordinary = await adapter.coord_board("agent-ssh-gateway", limit=1)
    assert ordinary["ok"] is True
    assert "resume" not in ordinary["result"]
    client.get_repo.assert_not_awaited()
    client.list_pull_requests.assert_not_awaited()
    result = await adapter.coord_board("agent-ssh-gateway", limit=1, include_summary=True)
    assert result["ok"] is True, result
    data = result["result"]
    assert data["count"] == 1
    resume = data["resume"]
    assert resume["authorizes_mutation"] is False
    assert resume["atomic_snapshot"] is False
    assert "secret@example" not in str(resume)
    assert "PRIVATE BODY" not in str(resume)
    assert "SECRET PROVIDER" not in str(resume)
    if scenario == "wrong_repo":
        assert resume["repo_state"] == "identity_unverified"
        assert resume["open_issues_count"] is None
        assert resume["default_head_sha"] is None
        client.get_branch.assert_not_awaited()
        client.list_pull_requests.assert_not_awaited()
        client.list_action_runs.assert_not_awaited()
        return
    assert resume["repo_state"] == "observed"
    assert resume["open_issues_count"] == 48
    assert resume["default_head_sha"] == master_sha
    assert resume["default_branch"] == "master"
    client.get_branch.assert_awaited_once_with("gpakoh", "agent-ssh-gateway", "master")
    client.list_pull_requests.assert_awaited_once_with("gpakoh", "agent-ssh-gateway", state="open", limit=10)
    client.list_action_runs.assert_awaited_once_with("gpakoh", "agent-ssh-gateway", limit=20)
    assert resume["open_prs_state"] == ("partial" if scenario in {"full_page", "malformed_pr"} else "observed")
    assert resume["open_prs"][0]["head_sha"] == feature_sha
    assert len(resume["open_prs"]) == (10 if scenario == "full_page" else 1)
    if scenario == "failed_runs":
        assert resume["ci_state"] == "unavailable"
        assert resume["default_head_runs"] == []
        assert resume["open_prs"][0]["runs"] == []
    else:
        assert resume["ci_state"] == ("partial" if scenario == "wrong_run_repo" else "observed")
        assert resume["ci_window_truncated"] is True
        assert [r["id"] for r in resume["default_head_runs"]] == [101]
        assert [r["id"] for r in resume["open_prs"][0]["runs"]] == ([] if scenario == "wrong_run_repo" else [102])
    assert client.comment_calls == 0


@pytest.mark.asyncio
async def test_opt_in_summary_preserves_unknown_state_as_lower_bound(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    for i in range(3):
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway",
            source_project="agent-ssh-gateway", title=f"Unknown {i}",
            body="Evidence", request_key=f"summary-unknown-{i}-123",
        )
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )
    observed = []

    async def verified(_client, _owner, _repo, number):
        observed.append(number)
        return (None, True) if number == 2 else ("open", False)

    monkeypatch.setattr(adapter, "preview_thread_state", verified)
    for reader in (adapter.coord_inbox, adapter.coord_board):
        observed.clear()
        data = (await reader("agent-ssh-gateway", limit=2, include_summary=True))["result"]
        assert data["summary"]["count_exact"] is False
        assert data["summary"]["verified_thread_count"] == 2
        assert data["summary"]["unverified_thread_numbers"] == [2]
        assert data["summary"]["state_counts"]["open"] == {"count": 2, "count_exact": False}
        assert data["summary"]["unexamined_thread_count"] == 0
        assert data["summary"]["source_scan_truncated"] is False
        assert set(observed) == {1, 2, 3}
        assert all(not c["count_exact"] for c in data["summary"]["state_counts"].values())
    for invalid in (1, "true", None):
        for reader in (adapter.coord_inbox, adapter.coord_board):
            denied = await reader("agent-ssh-gateway", include_summary=invalid)
            assert denied["ok"] is False
            assert denied["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_opt_in_summary_caps_work_and_never_claims_exact_large_totals(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )

    async def large_scan(_client, _owner, _repo):
        return [
            {
                "number": i + 1, "title": f"Issue {i}", "state": "open",
                "url": f"https://gitea.example/issues/{i + 1}",
                "source_project": "agent-ssh-gateway",
                "request_key": f"large-summary-{i}-123",
                "updated_at": "2026-11-03T09:00:00Z",
            }
            for i in range(105)
        ], False

    checked = []

    async def verify(_client, _owner, _repo, number):
        checked.append(number)
        return "accepted", False

    monkeypatch.setattr(adapter, "list_threads_preview", large_scan)
    monkeypatch.setattr(adapter, "preview_thread_state", verify)
    for reader in (adapter.coord_inbox, adapter.coord_board):
        checked.clear()
        result = await reader("agent-ssh-gateway", limit=1, include_summary=True)
        assert result["ok"] is True, result
        summary = result["result"]["summary"]
        assert summary["observed_thread_count"] == 105
        assert summary["examined_thread_count"] == adapter._SUMMARY_MAX_THREADS
        assert summary["verified_thread_count"] == adapter._SUMMARY_MAX_THREADS
        assert summary["unexamined_thread_count"] == 55
        assert summary["count_exact"] is False
        assert summary["state_counts"]["accepted"]["count"] == 50
        assert max(checked) <= 50
        assert len(checked) <= 51  # One ordinary preview + 50 summary probes.

    async def capped_scan(_client, _owner, _repo):
        threads, _ = await large_scan(_client, _owner, _repo)
        return threads[:1], True

    monkeypatch.setattr(adapter, "list_threads_preview", capped_scan)
    data = (await adapter.coord_board(
        "agent-ssh-gateway", limit=1, include_summary=True
    ))["result"]
    assert data["summary"]["source_scan_truncated"] is True
    assert data["summary"]["count_exact"] is False
    assert data["summary"]["state_counts"]["accepted"]["count_exact"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_name", ["coord_board", "coord_inbox"])
async def test_summary_deadline_preserves_completed_counts_and_resume(monkeypatch, reader_name):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    monkeypatch.setattr(adapter, "_SUMMARY_READ_TIMEOUT", 0.1, raising=False)
    client = FakeClient()
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )
    rows = [
        {"number": i, "title": f"Issue {i}", "state": "open",
         "url": f"https://gitea.example/issues/{i}",
         "source_project": "agent-ssh-gateway", "request_key": f"deadline-{i}-123",
         "updated_at": "2026-11-03T09:00:00Z"}
        for i in range(1, 5)
    ]
    monkeypatch.setattr(adapter, "list_threads_preview", AsyncMock(return_value=(rows, False)))
    cancelled = asyncio.Event()
    calls = []

    async def verify(_client, _owner, _repo, number):
        calls.append(number)
        if number == 2:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return "accepted", False

    monkeypatch.setattr(adapter, "preview_thread_state", verify)
    resume = {"repo_state": "observed", "open_issues_count": 48}
    monkeypatch.setattr(adapter, "_project_resume", AsyncMock(return_value=resume))
    reader = getattr(adapter, reader_name)
    result = await asyncio.wait_for(reader(
        "agent-ssh-gateway", limit=1, include_summary=True
    ), timeout=2)
    assert result["ok"] is True, result
    data = result["result"]
    summary = data["summary"]
    assert summary["state_counts"]["accepted"]["count"] == 1
    assert summary["count_exact"] is False
    assert summary["observed_thread_count"] == 4
    assert summary["examined_thread_count"] == 2
    assert summary["verified_thread_count"] == 1
    assert summary["unexamined_thread_count"] == 2
    assert summary["unverified_thread_numbers"] == [2]
    assert summary["scan_deadline_reached"] is True
    assert summary["timed_out_thread_number"] == 2
    assert calls == [1, 1, 2]
    assert cancelled.is_set()
    if reader_name == "coord_board":
        assert data["resume"] == resume
    else:
        assert "resume" not in data


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_name", ["coord_board", "coord_inbox"])
async def test_preview_provider_failure_preserves_other_verified_threads(monkeypatch, reader_name):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    for number in range(1, 4):
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway",
            source_project="agent-ssh-gateway", title=f"Issue {number}",
            body="Evidence", request_key=f"preview-provider-failure-{number}",
        )
    await core.write_event(
        client, "gpakoh", "agent-ssh-gateway", 3,
        text="Acknowledged", state="accepted", request_key="preview-provider-accepted",
    )
    original_get_issue = client.get_issue

    async def get_issue(owner, repo, number):
        if number == 2:
            raise OSError("PRIVATE PROVIDER ERROR")
        return await original_get_issue(owner, repo, number)

    client.get_issue = get_issue
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )
    response = await getattr(adapter, reader_name)("agent-ssh-gateway", limit=3)
    assert response["ok"] is True, response
    data = response["result"]
    assert data["count"] == 2
    assert data["count_exact"] is False
    assert data["truncated"] is True
    assert data["examined_thread_count"] == 3
    assert data["incomplete_thread_numbers"] == [2]
    assert data["next_offset"] is None
    if reader_name == "coord_board":
        assert [col["count"] for col in data["columns"]] == [1, 1, 0, 0]
        assert all(col["count_exact"] is False for col in data["columns"])
        assert [card["number"] for col in data["columns"] for card in col["cards"]] == [1, 3]
    else:
        assert {row["number"]: row["coord_state"] for row in data["threads"]} == {
            1: "open", 3: "accepted",
        }
    assert "PRIVATE PROVIDER ERROR" not in str(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_name", ["coord_board", "coord_inbox"])
async def test_preview_deadline_preserves_rows_and_next_page(monkeypatch, reader_name):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    for number in range(1, 4):
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway",
            source_project="agent-ssh-gateway", title=f"Issue {number}",
            body="Evidence", request_key=f"preview-deadline-{number}",
        )
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(adapter, "server_attr", lambda name: (lambda _: client) if name == "GiteaClient" else None)
    monkeypatch.setattr(adapter, "_PREVIEW_READ_TIMEOUT", 0.02, raising=False)
    checked = []

    async def verify(_client, _owner, _repo, number):
        checked.append(number)
        if number == 2:
            await asyncio.sleep(0.1)
        return "open", False

    monkeypatch.setattr(adapter, "preview_thread_state", verify)
    reader = getattr(adapter, reader_name)
    first = (await reader("agent-ssh-gateway", limit=3))["result"]
    assert first["count"] == 1
    assert first["count_exact"] is False
    assert first["incomplete_thread_numbers"] == [2]
    assert first["preview_deadline_reached"] is True
    assert first["preview_timed_out_thread_number"] == 2
    assert first["next_offset"] == 2
    assert checked == [1, 2]
    second = (await reader("agent-ssh-gateway", limit=3, offset=2))["result"]
    assert second["count"] == 1 and second["next_offset"] is None
    assert second["preview_deadline_reached"] is False
    assert second["preview_timed_out_thread_number"] is None
    assert checked == [1, 2, 3]


@pytest.mark.asyncio
async def test_preview_caller_cancellation_propagates(monkeypatch):
    async def cancelled(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(adapter, "preview_thread_state", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await adapter._verified_preview(
            FakeClient(), "gpakoh", "agent-ssh-gateway", [{"number": 1}], 1,
        )


@pytest.mark.asyncio
async def test_summary_provider_failure_preserves_other_verified_counts(monkeypatch):
    async def verify(_client, _owner, _repo, number):
        if number == 2:
            raise OSError("PRIVATE PROVIDER ERROR")
        return "accepted", False

    monkeypatch.setattr(adapter, "preview_thread_state", verify)
    data = await adapter._verified_global_summary(
        FakeClient(), "gpakoh", "agent-ssh-gateway",
        [{"number": i} for i in range(1, 4)], False,
    )
    assert data["verified_thread_count"] == 2
    assert data["examined_thread_count"] == 3
    assert data["unverified_thread_numbers"] == [2]
    assert data["probe_error_thread_numbers"] == [2]
    assert data["scan_deadline_reached"] is False
    assert data["count_exact"] is False
    assert "PRIVATE" not in str(data)


@pytest.mark.asyncio
async def test_summary_caller_cancellation_is_not_partial_success(monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def verify(*_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(adapter, "preview_thread_state", verify)
    task = asyncio.create_task(adapter._verified_global_summary(
        FakeClient(), "gpakoh", "agent-ssh-gateway", [{"number": 1}], False,
    ))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_board_and_inbox_bounded_offset_pagination_without_false_totals(monkeypatch):
    """Every verified page is reachable; no page claims global exact counts."""
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    for index in range(5):
        key = f"paged-thread-issue-{index}-123"
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway",
            source_project="agent-ssh-gateway",
            title=f"Paged {index}", body="Evidence", request_key=key,
        )
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )

    for reader in (adapter.coord_inbox, adapter.coord_board):
        position = 0
        numbers = []
        for expected_next in (2, 4, None):
            response = await reader("agent-ssh-gateway", limit=2, offset=position)
            assert response["ok"] is True, response
            data = response["result"]
            assert data["offset"] == position
            assert data["next_offset"] == expected_next
            assert data["source_scan_truncated"] is False
            assert data["count_exact"] is False
            assert data["truncated"] is True
            assert data["examined_thread_count"] == (2 if expected_next else 1)
            if "threads" in data:
                numbers.extend(t["number"] for t in data["threads"])
            else:
                numbers.extend(c["number"] for col in data["columns"] for c in col["cards"])
                assert all(col["count_exact"] is False for col in data["columns"])
            if expected_next is not None:
                position = expected_next
        assert sorted(numbers) == [1, 2, 3, 4, 5]
        assert len(set(numbers)) == 5
        for invalid in (-1, 500, True, "1"):
            denied = await reader("agent-ssh-gateway", limit=2, offset=invalid)
            assert denied["ok"] is False


@pytest.mark.asyncio
async def test_bounded_offset_skips_unknown_state_without_losing_following_threads(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    for index in range(4):
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway",
            source_project="agent-ssh-gateway",
            title=f"Thread {index}", body="Evidence",
            request_key=f"skip-long-history-{index}-123",
        )
    original_comments = client.list_issue_timeline

    async def repeated_comments(owner, repo, number, *, limit=50, page=1):
        if number == 1:
            return [{"id": x + 1, "type": "comment", "body": "untrusted",
                     "user": {"login": "other"}} for x in range(50)]
        return await original_comments(owner, repo, number, limit=limit, page=page)

    client.list_issue_timeline = repeated_comments
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )
    for reader in (adapter.coord_inbox, adapter.coord_board):
        first = (await reader("agent-ssh-gateway", limit=1))["result"]
        assert first["incomplete_thread_numbers"] == [1]
        assert first["examined_thread_count"] == 2
        assert first["next_offset"] == 2
        second = (await reader("agent-ssh-gateway", limit=1, offset=2))["result"]
        assert second["incomplete_thread_numbers"] == []
        assert second["next_offset"] == 3
        assert second["count_exact"] is False
        third = (await reader("agent-ssh-gateway", limit=1, offset=3))["result"]
        assert third["next_offset"] is None
        assert third["count_exact"] is False


@pytest.mark.asyncio
async def test_paginated_board_explicitly_marks_source_window_exhaustion(monkeypatch):
    """The 500-Issue ceiling is not a verified global total or a write lease."""
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )
    async def capped_scan(_client, _owner, _repo):
        return [
            {
                "number": i + 1, "title": f"Thread {i + 1}", "state": "open",
                "url": f"https://git.example/issues/{i + 1}",
                "source_project": "agent-ssh-gateway",
                "request_key": f"bounded-thread-{i + 1}-123",
                "updated_at": "2026-11-02T12:00:00Z",
            }
            for i in range(500)
        ], True

    seen = []

    async def verify(_client, _owner, _repo, number):
        seen.append(number)
        return "open", False

    monkeypatch.setattr(adapter, "list_threads_preview", capped_scan)
    monkeypatch.setattr(adapter, "preview_thread_state", verify)
    for reader in (adapter.coord_board, adapter.coord_inbox):
        seen.clear()
        data = (await reader("agent-ssh-gateway", limit=2, offset=498))["result"]
        assert seen == [499, 500]
        assert data["count"] == 2
        assert data["offset"] == 498
        assert data["next_offset"] is None
        assert data["source_scan_truncated"] is True
        assert data["truncated"] is True
        assert data["count_exact"] is False


@pytest.mark.asyncio
async def test_coord_open_repeated_comment_page_is_bounded_and_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only can show evidence while writes still reject an incomplete journal."""
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="agent-ssh-gateway", title="Long history",
        body="Evidence", request_key="long-history-thread-123",
    )
    repeated = [
        {
            "id": n + 1,
            "type": "comment",
            "body": "untrusted note",
            "user": {"login": "other"},
        }
        for n in range(50)
    ]
    requested_pages: list[int] = []

    async def stuck_comments(owner, repo, number, *, limit=50, page=1):
        requested_pages.append(page)
        assert number == 1
        return repeated

    async def enter(self):
        return self

    async def leave(self, *args):
        return None

    client.list_issue_timeline = stuck_comments
    monkeypatch.setattr(FakeClient, "__aenter__", enter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", leave, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )

    response = await adapter.coord_open("agent-ssh-gateway", 1)
    assert response["ok"] is True, response
    detail = response["result"]["thread"]
    assert detail["number"] == 1
    assert detail["state"] == "open"
    assert detail["history_truncated"] is True
    assert detail["history_incomplete_reason"] == "non_progressing_comment_cursor"
    assert detail["history_pages_scanned"] == 2
    assert detail["coord_state"] is None
    assert len(detail["history"]) == 50
    assert len({entry["id"] for entry in detail["history"]}) == 50
    assert "close_state_sha256" not in detail
    assert "close_content_sha256" not in detail
    assert requested_pages == [1, 2]

    # The same fake server must never authorize a new comment based on the
    # preview: real write idempotency still requires full history.
    with pytest.raises(core.CoordinationOutcomeUnknown, match="5000-row"):
        await core.write_event(
            client, "gpakoh", "agent-ssh-gateway", 1,
            text="Do not append", request_key="long-history-deny-123", state="message",
        )
    assert client.comment_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_ids", "expected_reason", "expected_count", "incomplete"),
    [
        (list(range(1, 51)), "non_progressing_comment_cursor", 50, True),
        (list(range(26, 76)), "overlapping_comment_pages", 75, True),
        (list(range(51, 101)), "bounded_comment_window", 100, True),
        ([51], None, 51, False),
        ([75, 51], "non_monotonic_comment_cursor", 52, True),
        ([None], "unverifiable_timeline_event", 50, True),
    ],
)
async def test_readonly_comment_preview_explains_page_progress(
    second_ids, expected_reason, expected_count, incomplete
):
    pages = []
    async def fetch(page):
        pages.append(page)
        ids = list(range(1, 51)) if page == 1 else second_ids
        return [
            {"id": ident, "type": "comment", "body": "evidence", "user": {"login": "gpakoh"}}
            for ident in ids
        ]

    rows, truncated, reason, count_pages = await coordination_engine._timeline_comment_pages(fetch)
    assert truncated is incomplete
    assert reason == expected_reason
    assert len(rows) == expected_count
    assert count_pages == 2
    assert pages == [1, 2]


@pytest.mark.asyncio
async def test_short_unverifiable_comment_id_never_receives_readonly_close_lease():
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="agent-ssh-gateway", title="Unverifiable history",
        body="Evidence", request_key="unverifiable-history-123",
    )
    client.comments[1] = [
        {"id": None, "body": "not verifiable", "user": {"login": "gpakoh"}}
    ]
    detail = await core.open_thread(
        client, "gpakoh", "agent-ssh-gateway", 1, allow_partial=True
    )
    assert detail["history_truncated"] is True
    assert detail["history_incomplete_reason"] == "unverifiable_timeline_event"
    assert detail["history_pages_scanned"] == 1
    assert detail["coord_state"] is None
    assert "close_state_sha256" not in detail
    assert "close_content_sha256" not in detail
    state, incomplete = await core.preview_thread_state(
        client, "gpakoh", "agent-ssh-gateway", 1
    )
    assert state is None and incomplete is True


@pytest.mark.asyncio
async def test_null_timeline_with_verified_zero_comments_is_empty_not_an_error():
    """Gitea can return JSON null for a timeline with no events."""
    class NullTimeline(FakeClient):
        async def list_issue_timeline(self, owner, repo, number, *, limit=50, page=1):
            return None

    client = NullTimeline()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="agent-ssh-gateway", title="No comments",
        body="New coordination issue", request_key="null-timeline-empty-123",
    )
    client.issues[0]["comments"] = 0

    partial = await core.open_thread(
        client, "gpakoh", "agent-ssh-gateway", 1, allow_partial=True
    )
    assert partial["history"] == []
    assert partial["history_truncated"] is False
    assert partial["history_incomplete_reason"] is None
    assert partial["history_pages_scanned"] == 1
    assert partial["coord_state"] == "open"
    assert "close_state_sha256" in partial

    strict = await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)
    assert strict["coord_state"] == "open"
    assert "close_state_sha256" in strict
    assert await core.preview_thread_state(
        client, "gpakoh", "agent-ssh-gateway", 1
    ) == ("open", False)


@pytest.mark.asyncio
async def test_zero_comment_count_cannot_authorize_conflicting_timeline_comment():
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="agent-ssh-gateway", title="Conflicting count",
        body="Untrusted history", request_key="timeline-count-conflict-123",
    )
    client.issues[0]["comments"] = 0
    client.comments[1] = [
        {"id": 1, "type": "comment", "body": core._header(
            kind="event", key="spoofed-count-state-123", state="resolved"
        ) + "\nDo not trust", "user": {"login": "gpakoh"}}
    ]
    partial = await core.open_thread(
        client, "gpakoh", "agent-ssh-gateway", 1, allow_partial=True
    )
    assert partial["history_truncated"] is True
    assert partial["coord_state"] is None
    assert "close_state_sha256" not in partial
    with pytest.raises(core.CoordinationOutcomeUnknown, match="invalid"):
        await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, None, True])
async def test_null_timeline_without_verified_zero_comments_fails_closed(count):
    class NullTimeline(FakeClient):
        async def list_issue_timeline(self, owner, repo, number, *, limit=50, page=1):
            return None

    client = NullTimeline()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="agent-ssh-gateway", title="Unverified history",
        body="Uncertain", request_key="null-timeline-unknown-123",
    )
    if count is not None:
        client.issues[0]["comments"] = count

    partial = await core.open_thread(
        client, "gpakoh", "agent-ssh-gateway", 1, allow_partial=True
    )
    assert partial["history_truncated"] is True
    assert partial["history_incomplete_reason"] == "unverifiable_null_timeline_page"
    assert partial["coord_state"] is None
    assert "close_state_sha256" not in partial
    assert await core.preview_thread_state(
        client, "gpakoh", "agent-ssh-gateway", 1
    ) == (None, True)
    with pytest.raises(core.CoordinationOutcomeUnknown, match="null"):
        await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)


@pytest.mark.asyncio
async def test_timeline_service_only_first_page_keeps_later_coordination_event():
    """Count raw timeline events, not filtered comments, to detect next page."""
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
        title="Interleaved timeline", body="Review", request_key="timeline-interleaved-123",
    )
    spoof = core._header(kind="event", key="timeline-spoof-123", state="resolved")
    accepted = core._header(kind="event", key="timeline-valid-123", state="accepted")
    client.comments[1] = [
        {"id": i, "type": "label", "body": spoof, "user": {"login": "gpakoh"}}
        for i in range(1, 51)
    ] + [
        {"id": 51, "type": "comment", "body": accepted + "\nAccepted",
         "user": {"login": "gpakoh"}}
    ]
    # The service-only first page is FULL, so it cannot prove the end.
    full = await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)
    assert full["coord_state"] == "accepted"
    assert [entry["id"] for entry in full["history"]] == [51]
    assert full["history"][0]["request_key"] == "timeline-valid-123"
    assert "close_state_sha256" in full
    state, incomplete = await core.preview_thread_state(
        client, "gpakoh", "agent-ssh-gateway", 1
    )
    assert (state, incomplete) == ("accepted", False)


@pytest.mark.asyncio
async def test_timeline_bound_counts_unfiltered_service_events():
    """The two-page preview cannot hide 100 activity events before a comment."""
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
        title="Untrusted end", body="Review", request_key="timeline-bound-123",
    )
    accepted = core._header(kind="event", key="timeline-late-123", state="accepted")
    client.comments[1] = [
        {"id": i, "type": "close", "body": accepted, "user": {"login": "gpakoh"}}
        for i in range(1, 101)
    ] + [
        {"id": 101, "type": "comment", "body": accepted + "\nAccepted",
         "user": {"login": "gpakoh"}}
    ]
    partial = await core.open_thread(
        client, "gpakoh", "agent-ssh-gateway", 1, allow_partial=True
    )
    assert partial["history"] == []
    assert partial["history_incomplete_reason"] == "bounded_comment_window"
    assert partial["history_pages_scanned"] == 2
    assert partial["coord_state"] is None
    assert "close_state_sha256" not in partial
    complete = await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)
    assert complete["coord_state"] == "accepted"
    assert len(complete["history"]) == 1


@pytest.mark.asyncio
async def test_timeline_invalid_type_and_duplicate_service_event_fail_closed():
    for problem in ("missing_type", "duplicate_id"):
        client = FakeClient()
        await core.create_thread(
            client, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
            title="Invalid timeline", body="Review",
            request_key=f"invalid-timeline-{problem}-123",
        )
        if problem == "missing_type":
            client.comments[1] = [{"id": 1, "type": "", "body": "not verifiable"}]
        else:
            client.comments[1] = [
                {"id": i, "type": "label"} for i in range(1, 51)
            ] + [{"id": 50, "type": "comment", "body": "malicious",
                  "user": {"login": "gpakoh"}}]
        partial = await core.open_thread(
            client, "gpakoh", "agent-ssh-gateway", 1, allow_partial=True
        )
        assert partial["history_truncated"] is True
        assert partial["coord_state"] is None
        assert "close_state_sha256" not in partial
        with pytest.raises(core.CoordinationOutcomeUnknown):
            await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)


@pytest.mark.asyncio
async def test_timeline_strict_scan_stops_at_5000_raw_events_even_if_no_comments():
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
        title="Bounded activity", body="Review", request_key="timeline-5000-123",
    )
    client.comments[1] = [{"id": i, "type": "label"} for i in range(1, 5001)]
    with pytest.raises(core.CoordinationOutcomeUnknown, match="5000-row"):
        await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)


@pytest.mark.asyncio
async def test_real_gitea_transport_pages_timeline_before_filtering():
    """Mock the actual HTTP API: a full service-event page is not EOF."""
    import httpx

    from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

    fake = FakeClient()
    await core.create_thread(
        fake, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
        title="Transport paging", body="Review", request_key="transport-timeline-123",
    )
    event = core._header(kind="event", key="transport-timeline-accepted-123", state="accepted")
    requested = []

    def handle(request):
        path = request.url.path
        if path.endswith("/timeline"):
            assert request.url.params["limit"] == "50"
            page = int(request.url.params["page"])
            requested.append(page)
            if page == 1:
                return httpx.Response(200, json=[
                    {"id": i, "type": "label", "body": event,
                     "user": {"login": "gpakoh"}}
                    for i in range(1, 51)
                ])
            assert page == 2
            return httpx.Response(200, json=[
                {"id": 51, "type": "comment", "body": event + "\nAccepted",
                 "user": {"login": "gpakoh"}}
            ])
        if path.endswith("/user"):
            return httpx.Response(200, json={"login": "gpakoh"})
        if path.endswith("/issues/1"):
            return httpx.Response(200, json=fake.issues[0])
        raise AssertionError(f"unexpected Gitea API path: {path}")

    client = GiteaClient("fake-token")
    await client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="https://gitea.invalid/api/v1/",
    )
    try:
        thread = await core.open_thread(
            client, "gpakoh", "agent-ssh-gateway", fake.issues[0]["number"]
        )
        assert thread["coord_state"] == "accepted"
        assert [entry["id"] for entry in thread["history"]] == [51]
        assert thread["history"][0]["request_key"] == "transport-timeline-accepted-123"
        assert requested == [1, 2]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_lowlevel_timeline_uses_documented_page_endpoint_only(monkeypatch):
    from examples.mcp_client_remote.fleet.gitea_client import (
        ALLOWED_ENDPOINTS,
        GiteaClient,
    )

    client = GiteaClient("fake-token")
    get = AsyncMock(return_value=[{"type": "comment", "id": 1}])
    monkeypatch.setattr(client, "_get", get)
    try:
        rows = await client.list_issue_timeline(
            "gpakoh", "agent-ssh-gateway", 42, limit=50, page=2
        )
        assert rows == [{"type": "comment", "id": 1}]
        get.assert_awaited_once_with(
            "/repos/{owner}/{repo}/issues/{number}/timeline",
            params={"limit": 50, "page": 2},
            owner="gpakoh", repo="agent-ssh-gateway", number=42,
        )
        assert "/repos/{owner}/{repo}/issues/{number}/timeline" in ALLOWED_ENDPOINTS
        assert "/repos/{owner}/{repo}/issues/{number}/comments" not in ALLOWED_ENDPOINTS
        for kwargs in ({"page": 0}, {"page": True}, {"limit": 51}, {"number": -1}):
            with pytest.raises(ValueError):
                await client.list_issue_timeline(
                    "gpakoh", "agent-ssh-gateway", kwargs.pop("number", 42), **kwargs
                )
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["complete", "count_mismatch", "drift", "bounded", "oversized"])
async def test_mcp_coord_open_recovers_recent_evidence_without_close_authority(
    monkeypatch, scenario,
):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="agent-ssh-gateway", title="Long handoff",
        body="Evidence", request_key="recent-history-thread-123",
    )
    total = 501 if scenario == "bounded" else 125
    client.comments[1] = [
        {"id": i, "body": f"Evidence {i}", "user": {"login": "other"}}
        for i in range(1, total + 1)
    ]
    client.comments[1][-1]["body"] = (
        core._header(kind="event", key="recent-history-accept-123", state="accepted")
        + "\nLatest handoff"
    )
    client.comments[1][-1]["user"] = {"login": "gpakoh"}
    if scenario == "oversized":
        client.comments[1][-1]["body"] += "x" * 20000 + " LAST CHECKPOINT"
    client.issues[0]["comments"] = total + (scenario == "count_mismatch")
    client.issues[0]["updated_at"] = "initial"
    original_get = client.get_issue
    issue_reads = 0

    async def get_issue(owner, repo, number):
        nonlocal issue_reads
        issue_reads += 1
        issue = dict(await original_get(owner, repo, number))
        if scenario == "drift" and issue_reads > 1:
            issue["updated_at"] = "changed"
        return issue

    async def enter(self):
        return self

    async def leave(self, *args):
        return None

    client.get_issue = get_issue
    monkeypatch.setattr(FakeClient, "__aenter__", enter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", leave, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )
    response = await adapter.coord_open("agent-ssh-gateway", 1)
    assert response["ok"] is True, response
    detail = response["result"]["thread"]
    complete = scenario in {"complete", "oversized"}
    expected_last = 500 if scenario == "bounded" else total
    assert detail["history_scan_complete"] is complete
    assert detail["history_last_scanned_event_id"] == expected_last
    assert detail["history"][-1]["id"] == expected_last
    assert 1 <= len(detail["history"]) <= 50
    assert detail["history_display_omitted_count"] == expected_last - len(detail["history"])
    assert detail["history_truncated"] is True
    assert detail["coord_state"] is None
    assert "close_state_sha256" not in detail
    assert "close_content_sha256" not in detail
    assert detail["history_pages_scanned"] == (10 if scenario == "bounded" else 3)
    assert detail["history_incomplete_reason"] == {
        "complete": "bounded_history_display",
        "count_mismatch": "comment_count_mismatch",
        "drift": "issue_changed_during_scan",
        "bounded": "bounded_comment_window",
        "oversized": "bounded_history_display",
    }[scenario]
    if complete:
        assert detail["history_latest_event_id"] == total
        assert detail["history"][-1]["state"] == "accepted"
    else:
        assert "history_latest_event_id" not in detail
    if scenario == "oversized":
        assert detail["history"][-1]["body"].endswith(" LAST CHECKPOINT")
        assert detail["history"][-1]["body_truncated"] is True
        assert len(detail["history"][-1]["body"]) <= 16000
    assert client.comment_calls == 0


@pytest.mark.asyncio
async def test_coord_open_history_cursor_recovers_all_events_and_body_chunks(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(adapter, "_registered", lambda p: core.validate_project(p))
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
        title="Navigable history", body="Evidence", request_key="nav-history-thread-123",
    )
    bodies = {i: f"Evidence {i}" for i in range(1, 126)}
    bodies[125] = "first " + "x" * 34000 + " last"
    client.comments[1] = [
        {"id": i, "body": body, "user": {"login": "other"}}
        for i, body in bodies.items()
    ]
    client.issues[0]["comments"] = len(bodies)
    client.issues[0]["updated_at"] = "initial"
    monkeypatch.setattr(FakeClient, "__aenter__", _fake_aenter, raising=False)
    monkeypatch.setattr(FakeClient, "__aexit__", _fake_aexit, raising=False)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda _: client) if name == "GiteaClient" else None,
    )
    first = (await adapter.coord_open("agent-ssh-gateway", 1))["result"]["thread"]
    cursor = first["history_older_cursor"]
    assert cursor
    seen = []
    pages = 0
    page = first
    while True:
        pages += 1
        assert pages < 15
        assert page["history_scan_complete"] is True
        assert page["history_truncated"] is True
        assert page["coord_state"] is None
        assert "close_state_sha256" not in page
        assert "close_content_sha256" not in page
        assert len(page["history"]) <= 50
        assert sum(len(entry["body"]) for entry in page["history"]) <= 16000
        for entry in reversed(page["history"]):
            seen.append((entry["id"], entry.get("body_start", 0), entry["body"]))
        cursor = page["history_older_cursor"]
        if cursor is None:
            break
        result = await adapter.coord_open("agent-ssh-gateway", 1, history_cursor=cursor)
        assert result["ok"] is True, result
        page = result["result"]["thread"]
    reconstructed = {}
    for identifier, start, body in sorted(seen):
        assert start == len(reconstructed.get(identifier, ""))
        reconstructed[identifier] = reconstructed.get(identifier, "") + body
    assert reconstructed == bodies
    assert client.comment_calls == 0

    # A changed comment with unchanged Issue metadata cannot reuse the cursor.
    client.comments[1][0]["body"] = "edited"
    result = await adapter.coord_open(
        "agent-ssh-gateway", 1, history_cursor=first["history_older_cursor"],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "cursor" in result["error"]["message"].lower()


@pytest.mark.asyncio
async def test_history_navigation_rejects_malformed_and_incomplete_scan(monkeypatch):
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway", source_project="agent-ssh-gateway",
        title="Incomplete history", body="Evidence", request_key="nav-incomplete-123",
    )
    client.comments[1] = [
        {"id": i, "body": "x", "user": {"login": "other"}} for i in range(1, 502)
    ]
    client.issues[0]["comments"] = 501
    page = await core.open_thread(
        client, "gpakoh", "agent-ssh-gateway", 1, allow_partial=True, recent_preview=True,
    )
    assert page["history_scan_complete"] is False
    assert page["history_older_cursor"] is None
    for cursor in ("", "bad", "0" * 64 + ":0:1", True):
        with pytest.raises(ValueError, match="cursor"):
            await core.open_thread(
                client, "gpakoh", "agent-ssh-gateway", 1,
                allow_partial=True, recent_preview=True, history_cursor=cursor,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "body_chars", "compact"),
    [(1, 16000, False), (1, 16001, True), (8, 2100, True),
     (50, 320, False), (51, 10, True), (54, 1200, True)],
)
async def test_coord_open_applies_display_budget_to_short_timeline(
    count, body_chars, compact,
):
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="agent-ssh-gateway", title="Short timeline",
        body="Evidence", request_key="short-display-budget-123",
    )
    bodies = {i: "x" * body_chars for i in range(1, count + 1)}
    client.comments[1] = [
        {"id": i, "body": body, "user": {"login": "other"}}
        for i, body in bodies.items()
    ]
    client.issues[0]["comments"] = count
    page = await core.open_thread(
        client, "gpakoh", "agent-ssh-gateway", 1,
        allow_partial=True, recent_preview=True,
    )
    assert page["history_pages_scanned"] <= 2
    assert page["history_scan_complete"] is True
    assert page["history_latest_event_id"] == count
    assert len(page["history"]) <= 50
    assert sum(len(entry["body"]) for entry in page["history"]) <= 16000
    assert page["history_truncated"] is compact
    assert bool(page["history_older_cursor"]) is compact
    assert ("close_content_sha256" in page) is not compact
    assert ("close_state_sha256" in page) is not compact
    if compact:
        assert page["coord_state"] is None
        assert page["history_incomplete_reason"] == "bounded_history_display"
    else:
        assert page["coord_state"] == "open"

    seen = []
    while True:
        seen.extend(
            (entry["id"], entry.get("body_start", 0), entry["body"])
            for entry in page["history"]
        )
        cursor = page["history_older_cursor"]
        if cursor is None:
            break
        page = await core.open_thread(
            client, "gpakoh", "agent-ssh-gateway", 1,
            allow_partial=True, recent_preview=True, history_cursor=cursor,
        )
        assert page["coord_state"] is None
        assert "close_state_sha256" not in page
        assert len(page["history"]) <= 50
        assert sum(len(entry["body"]) for entry in page["history"]) <= 16000
    reconstructed = {}
    for identifier, start, body in sorted(seen):
        assert start == len(reconstructed.get(identifier, ""))
        reconstructed[identifier] = reconstructed.get(identifier, "") + body
    assert reconstructed == bodies

    full = await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)
    assert full["history_truncated"] is False
    assert len(full["history"]) == count
    assert "close_state_sha256" in full
    assert "close_content_sha256" in full


@pytest.mark.asyncio
async def test_coord_open_short_history_retains_verified_state_and_close_lease():
    client = FakeClient()
    await core.create_thread(
        client, "gpakoh", "agent-ssh-gateway",
        source_project="agent-ssh-gateway", title="Small history",
        body="Evidence", request_key="short-history-thread-123",
    )
    await core.write_event(
        client, "gpakoh", "agent-ssh-gateway", 1,
        text="Ready", request_key="short-history-accept-123", state="accepted",
    )
    full = await core.open_thread(client, "gpakoh", "agent-ssh-gateway", 1)
    partial_allowed = await core.open_thread(
        client, "gpakoh", "agent-ssh-gateway", 1, allow_partial=True,
    )
    assert partial_allowed["history_truncated"] is False
    assert partial_allowed["history"] == full["history"]
    assert partial_allowed["coord_state"] == "accepted"
    assert partial_allowed["close_state_sha256"] == full["close_state_sha256"]


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


@pytest.mark.asyncio
async def test_per_thread_budget_keeps_preview_and_summary_progress(monkeypatch):
    monkeypatch.setattr(adapter, "_PREVIEW_READ_TIMEOUT", 0.5)
    monkeypatch.setattr(adapter, "_SUMMARY_READ_TIMEOUT", 0.5)
    monkeypatch.setattr(adapter, "_THREAD_READ_TIMEOUT", 0.02, raising=False)
    rows = [{"number": i} for i in range(1, 6)]
    cancelled = []

    async def verify(_client, _owner, _repo, number):
        if number in (1, 3):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(number)
        return "accepted", False

    monkeypatch.setattr(adapter, "preview_thread_state", verify)
    verified, incomplete, examined, deadline, last_timeout = await adapter._verified_preview(
        FakeClient(), "gpakoh", "agent-ssh-gateway", rows, 2,
    )
    assert [row["number"] for row in verified] == [2, 4]
    assert incomplete == [1, 3]
    assert examined == 4
    assert deadline is False
    assert last_timeout == 3
    assert cancelled == [1, 3]
    summary = await adapter._verified_global_summary(
        FakeClient(), "gpakoh", "agent-ssh-gateway", rows, False,
    )
    assert summary["verified_thread_count"] == 3
    assert summary["examined_thread_count"] == 5
    assert summary["unexamined_thread_count"] == 0
    assert summary["unverified_thread_numbers"] == [1, 3]
    assert summary["timed_out_thread_numbers"] == [1, 3]
    assert summary["timed_out_thread_number"] == 3
    assert summary["scan_deadline_reached"] is False
    assert summary["count_exact"] is False
    assert cancelled == [1, 3, 1, 3]
