"""Explicit close: actual filesystem receipts, Gitea state and HTTP boundary."""

from __future__ import annotations

import asyncio
import copy
import json
import multiprocessing

import pytest

from app.coordination_close import CloseRefused, _issue_lock, close_thread
from app.coordination_logic import CoordinationOutcomeUnknown, _header, open_thread

OWNER = "gpakoh"
REPO = "agent-ssh-gateway"
ACTOR = "0123456789abcdef"


class Client:
    def __init__(self):
        self.issue = {
            "id": 100,
            "number": 1,
            "title": "Completed fix",
            "state": "open",
            "updated_at": "2026-10-27T01:00:00Z",
            "html_url": "https://gitea.invalid/issues/1",
            "body": _header(
                kind="issue", key="close-test-issue", source=REPO, state="open", actor=ACTOR
            )
            + "\nDefect",
        }
        self.comments = [
            {
                "id": 7,
                "user": {"login": OWNER},
                "body": _header(
                    kind="event", key="close-test-report", state="resolved", actor=ACTOR
                )
                + "\nFixed in PR 42, exact-head CI passed. Native tests passed; candidate removed.",
                "created_at": "2026-10-27T01:00:00Z",
                "updated_at": "2026-10-27T01:00:00Z",
            }
        ]
        self.repo = {"full_name": OWNER + "/" + REPO, "permissions": {"push": True}}
        self.patch_calls = 0
        self.get_calls = 0
        self.before_get = None
        self.patch_hook = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_repo(self, owner, repo):
        return copy.deepcopy(self.repo)

    async def get_user(self):
        return {"login": OWNER}

    async def get_issue(self, owner, repo, number):
        assert (owner, repo, number) == (OWNER, REPO, 1)
        self.get_calls += 1
        if self.before_get:
            self.before_get(self.get_calls)
        return copy.deepcopy(self.issue)

    async def list_issue_comments(self, owner, repo, number, *, limit, page):
        return copy.deepcopy(self.comments[(page - 1) * limit : page * limit])

    async def close_coord_issue(self, owner, repo, number):
        self.patch_calls += 1
        if self.patch_hook:
            return await self.patch_hook()
        self.issue["state"] = "closed"
        self.issue["updated_at"] = "2026-10-27T02:00:00Z"
        return copy.deepcopy(self.issue)


async def request(client):
    thread = await open_thread(client, OWNER, REPO, 1)
    client.get_calls = 0
    return {
        "resolution_event_id": 7,
        "expected_state_sha256": thread["close_state_sha256"],
        "request_key": "explicit-close-test-001",
        "reason": "Operator reviewed the report and requested closure",
    }


async def close(client, directory, args, *, audit=lambda *args: None, actor=ACTOR):
    return await close_thread(
        client, OWNER, REPO, 1, **args, actor=actor, journal_dir=directory, audit=audit
    )


@pytest.mark.asyncio
async def test_close_uses_existing_report_and_replay_is_read_only(tmp_path):
    client = Client()
    args = await request(client)
    events = []
    result = await close(
        client, tmp_path / "receipts", args, audit=lambda *event: events.append(event)
    )
    assert result["closed"] and result["verified"] and not result["already_closed"]
    assert client.patch_calls == 1 and len(client.comments) == 1
    assert [e[0] for e in events] == ["intent", "success"]
    # New caller/session, same operation; report comment and PATCH are not duplicated.
    replay = await close(client, tmp_path / "receipts", args, actor="fedcba9876543210")
    assert replay["already_closed"] and client.patch_calls == 1 and len(client.comments) == 1


@pytest.mark.asyncio
async def test_existing_closed_issue_requires_reviewed_current_snapshot(tmp_path):
    client = Client()
    stale = await request(client)
    client.issue["state"] = "closed"
    with pytest.raises(CloseRefused, match="changed"):
        await close(client, tmp_path / "receipts", stale)
    result = await close(client, tmp_path / "receipts", await request(client))
    assert result["already_closed"] and client.patch_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reason", "resolution_event_id", "expected_state_sha256"])
async def test_same_key_different_payload_rejected(tmp_path, change):
    client = Client()
    args = await request(client)
    await close(client, tmp_path / "receipts", args)
    changed = dict(args)
    changed[change] = {
        "reason": "Different decision",
        "resolution_event_id": 8,
        "expected_state_sha256": "b" * 64,
    }[change]
    with pytest.raises(CloseRefused, match="different close payload"):
        await close(client, tmp_path / "receipts", changed)
    assert client.patch_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("resolution_event_id", True),
        ("resolution_event_id", 0),
        ("expected_state_sha256", "x"),
        ("expected_state_sha256", "A" * 64),
        ("request_key", "bad/key"),
        ("reason", ""),
        ("reason", "x" * 501),
    ],
)
async def test_invalid_inputs_have_no_mutation(tmp_path, field, value):
    client = Client()
    args = await request(client)
    args[field] = value
    with pytest.raises(ValueError):
        await close(client, tmp_path / "receipts", args)
    assert client.patch_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["pr", "ordinary", "forged", "not_resolved", "wrong_report"])
async def test_untrusted_or_wrong_resolution_is_not_a_close_permit(tmp_path, corrupt):
    client = Client()
    args = await request(client)
    if corrupt == "pr":
        client.issue["pull_request"] = {"url": "https://gitea.invalid/pulls/1"}
    elif corrupt == "ordinary":
        client.issue["body"] = "Unrelated issue"
    elif corrupt == "forged":
        client.comments[0]["user"]["login"] = "attacker"
    elif corrupt == "not_resolved":
        client.comments[0]["body"] = client.comments[0]["body"].replace(
            "state=resolved", "state=waiting_review"
        )
    else:
        args["resolution_event_id"] = 8
    with pytest.raises(ValueError):
        await close(client, tmp_path / "receipts", args)
    assert client.patch_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["title", "body", "comment", "reopen"])
async def test_thread_drift_before_patch_refused(tmp_path, change):
    client = Client()
    args = await request(client)

    def drift(call):
        if call != 2:
            return
        if change in ("title", "body"):
            client.issue[change] += " changed"
        elif change == "comment":
            client.comments.append({"id": 8, "body": "New concern", "user": {"login": "reviewer"}})
        else:
            client.issue["updated_at"] = "2026-10-27T02:00:00Z"

    client.before_get = drift
    with pytest.raises(CloseRefused, match="changed"):
        await close(client, tmp_path / "receipts", args)
    assert client.patch_calls == 0


@pytest.mark.asyncio
async def test_lost_patch_response_reconciles_closed_without_resending(tmp_path):
    client = Client()
    args = await request(client)

    async def lost():
        client.issue["state"] = "closed"
        raise OSError("transport lost; secret-token must not surface")

    client.patch_hook = lost
    result = await close(client, tmp_path / "receipts", args)
    assert result["closed"] and result["reconciled"] and client.patch_calls == 1


@pytest.mark.asyncio
async def test_uncertain_attempt_survives_restart_and_new_key_cannot_bypass_it(tmp_path):
    client = Client()
    args = await request(client)

    async def timeout():
        raise OSError("timeout")

    client.patch_hook = timeout
    with pytest.raises(CoordinationOutcomeUnknown, match="do not resend"):
        await close(client, tmp_path / "receipts", args)
    restarted = Client()  # no process-local idempotency cache or lock state
    with pytest.raises(CoordinationOutcomeUnknown, match="read-only"):
        await close(restarted, tmp_path / "receipts", args)
    with pytest.raises(CoordinationOutcomeUnknown, match="earlier close"):
        await close(
            restarted, tmp_path / "receipts", {**args, "request_key": "different-close-key"}
        )
    assert client.patch_calls == 1 and restarted.patch_calls == 0
    restarted.issue["state"] = "closed"  # late upstream completion; now provable
    assert (await close(restarted, tmp_path / "receipts", args))["reconciled"]
    assert restarted.patch_calls == 0


@pytest.mark.asyncio
async def test_cancelled_dispatched_operation_never_repatches(tmp_path):
    client = Client()
    args = await request(client)

    async def cancelled():
        raise asyncio.CancelledError

    client.patch_hook = cancelled
    with pytest.raises(asyncio.CancelledError):
        await close(client, tmp_path / "receipts", args)
    with pytest.raises(CoordinationOutcomeUnknown):
        await close(client, tmp_path / "receipts", args)
    assert client.patch_calls == 1


@pytest.mark.asyncio
async def test_reopen_after_completed_operation_is_not_automatically_closed_again(tmp_path):
    client = Client()
    args = await request(client)
    await close(client, tmp_path / "receipts", args)
    client.issue["state"] = "open"
    with pytest.raises(CoordinationOutcomeUnknown):
        await close(client, tmp_path / "receipts", args)
    assert client.patch_calls == 1


@pytest.mark.asyncio
async def test_remote_edit_in_patch_window_never_returns_verified_success(tmp_path):
    client = Client()
    args = await request(client)

    async def concurrent_edit():
        client.issue["state"] = "closed"
        client.comments.append({"id": 8, "body": "Reconsider", "user": {"login": "reviewer"}})

    client.patch_hook = concurrent_edit
    with pytest.raises(CoordinationOutcomeUnknown):
        await close(client, tmp_path / "receipts", args)
    assert client.issue["state"] == "closed"  # upstream has no Issue CAS; do not claim prevention
    assert client.patch_calls == 1


@pytest.mark.asyncio
async def test_permission_denial_records_attempt_without_retry(tmp_path):
    client = Client()
    args = await request(client)

    async def denied():
        raise PermissionError("HTTP 403")

    client.patch_hook = denied
    with pytest.raises(PermissionError):
        await close(client, tmp_path / "receipts", args)
    with pytest.raises(CoordinationOutcomeUnknown):
        await close(client, tmp_path / "receipts", args)
    assert client.patch_calls == 1


@pytest.mark.asyncio
async def test_concurrent_workers_cannot_dispatch_two_closes(tmp_path):
    client = Client()
    args = await request(client)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()
        client.issue["state"] = "closed"

    client.patch_hook = hold
    first = asyncio.create_task(close(client, tmp_path / "receipts", args))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        with pytest.raises(CloseRefused, match="Another close"):
            await close(
                client, tmp_path / "receipts", {**args, "request_key": "concurrent-other-key"}
            )
    finally:
        release.set()
        await first
    assert client.patch_calls == 1


@pytest.mark.asyncio
async def test_audit_failure_blocks_patch(tmp_path):
    client = Client()
    args = await request(client)

    def unavailable(*args):
        raise CloseRefused("AUDIT_UNAVAILABLE", "audit unavailable")

    with pytest.raises(CloseRefused, match="audit"):
        await close(client, tmp_path / "receipts", args, audit=unavailable)
    assert client.patch_calls == 0


@pytest.mark.asyncio
async def test_readback_failure_never_claims_success(tmp_path):
    client = Client()
    args = await request(client)

    def fail(call):
        if call > 2:
            raise OSError("readback unavailable")

    client.before_get = fail
    with pytest.raises(CoordinationOutcomeUnknown):
        await close(client, tmp_path / "receipts", args)
    assert client.patch_calls == 1 and client.issue["state"] == "closed"


@pytest.mark.asyncio
async def test_journal_contains_hashes_not_report_or_reason(tmp_path):
    client = Client()
    args = await request(client)
    await close(client, tmp_path / "receipts", args)
    journal = next((tmp_path / "receipts").glob("*/*.json")).read_text()
    assert client.comments[0]["body"] not in journal and args["reason"] not in journal
    assert json.loads(journal)["phase"] == "confirmed"


@pytest.mark.asyncio
async def test_symlink_receipt_directory_denied(tmp_path):
    client = Client()
    args = await request(client)
    (tmp_path / "actual").mkdir(mode=0o700)
    (tmp_path / "receipts").symlink_to(tmp_path / "actual", target_is_directory=True)
    with pytest.raises(CloseRefused):
        await close(client, tmp_path / "receipts", args)
    assert client.patch_calls == 0


@pytest.mark.asyncio
async def test_corrupt_receipt_never_authorizes_another_patch(tmp_path):
    client = Client()
    args = await request(client)
    await close(client, tmp_path / "receipts", args)
    path = next((tmp_path / "receipts").glob("*/*.json"))
    path.write_text("not JSON")
    with pytest.raises(CloseRefused):
        await close(client, tmp_path / "receipts", args)
    assert client.patch_calls == 1


def hold_lock_in_process(directory, connection):
    with _issue_lock(directory, OWNER, REPO, 1):
        connection.send("locked")
        if connection.poll(20):
            connection.recv()


@pytest.mark.asyncio
async def test_another_process_holding_issue_lock_blocks_dispatch(tmp_path):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=hold_lock_in_process, args=(tmp_path / "receipts", child))
    process.start()
    try:
        assert await asyncio.to_thread(parent.poll, 15), "lock worker did not start"
        assert parent.recv() == "locked"
        client = Client()
        with pytest.raises(CloseRefused, match="Another close"):
            await close(client, tmp_path / "receipts", await request(client))
        assert client.patch_calls == 0
    finally:
        parent.send("release")
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        parent.close()
        child.close()
    assert process.exitcode == 0


@pytest.mark.asyncio
async def test_attempt_is_on_disk_before_patch_and_fsync_failure_refuses_dispatch(
    tmp_path, monkeypatch
):
    from app import coordination_close as module

    client = Client()
    args = await request(client)

    async def inspect_intent():
        path = next((tmp_path / "receipts").glob("*/*.json"))
        assert json.loads(path.read_text())["phase"] == "dispatched"
        client.issue["state"] = "closed"

    client.patch_hook = inspect_intent
    assert (await close(client, tmp_path / "receipts", args))["closed"]
    failed = Client()
    failed_args = await request(failed)

    def no_durability(fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(module.os, "fsync", no_durability)
    with pytest.raises(OSError):
        await close(failed, tmp_path / "other-receipts", failed_args)
    assert failed.patch_calls == 0


@pytest.mark.asyncio
async def test_reconciled_completion_allows_later_explicit_reviewed_operation(tmp_path):
    client = Client()
    args = await request(client)

    async def uncertain():
        raise OSError("unknown")

    client.patch_hook = uncertain
    with pytest.raises(CoordinationOutcomeUnknown):
        await close(client, tmp_path / "receipts", args)
    client.issue["state"] = "closed"
    assert (await close(client, tmp_path / "receipts", args))["reconciled"]
    client.issue["state"] = "open"
    client.issue["updated_at"] = "2026-10-27T03:00:00Z"
    client.patch_hook = None
    reviewed = {**(await request(client)), "request_key": "new-reviewed-operation"}
    assert (await close(client, tmp_path / "receipts", reviewed))["closed"]
    assert client.patch_calls == 2  # second independent, explicitly reviewed close


def test_publication_requires_write_mode_and_admin_scope():
    from examples.mcp_server.tool_modes import should_register_tool
    from examples.mcp_server.tool_scopes import get_required_scopes, has_required_scope

    assert not should_register_tool("coord_close", "mcp_client")
    assert should_register_tool("coord_close", "mcp_client_write")
    assert get_required_scopes("coord_close") == ["mcp:repo", "mcp:admin"]
    assert not has_required_scope(["mcp:repo"], "coord_close")
    assert has_required_scope(["mcp:repo", "mcp:admin"], "coord_close")


@pytest.mark.asyncio
async def test_transport_patch_is_fixed_issue_endpoint_and_payload(monkeypatch):
    import httpx

    from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

    client = GiteaClient("not-a-real-token")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"number": 1, "state": "closed"})

    await client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gitea.invalid/api/v1/"
    )
    try:
        await client.close_coord_issue(OWNER, REPO, 1)
        assert len(calls) == 1 and calls[0].method == "PATCH"
        assert calls[0].url.path == "/api/v1/repos/gpakoh/agent-ssh-gateway/issues/1"
        assert json.loads(calls[0].content) == {"state": "closed"}
        with pytest.raises(ValueError, match="not allowed"):
            await client._patch(
                "/repos/{owner}/{repo}/issues/{number}",
                {"state": "closed"},
                owner=OWNER,
                repo=REPO,
                number=1,
            )
        for number in (True, 0, -1):
            with pytest.raises(ValueError):
                await client.close_coord_issue(OWNER, REPO, number)
        assert len(calls) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_transport_full_workflow_reconciles_actual_http_response_loss(tmp_path):
    import httpx

    from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

    state = Client()
    patches = []

    def handler(request):
        path = request.url.path
        if request.method == "PATCH":
            assert path == "/api/v1/repos/gpakoh/agent-ssh-gateway/issues/1"
            assert json.loads(request.content) == {"state": "closed"}
            patches.append(request)
            state.issue["state"] = "closed"
            raise httpx.ReadError("sensitive transport failure", request=request)
        if path == "/api/v1/user":
            return httpx.Response(200, json={"login": OWNER})
        if path.endswith("/comments"):
            return httpx.Response(200, json=state.comments)
        assert path == "/api/v1/repos/gpakoh/agent-ssh-gateway/issues/1"
        return httpx.Response(200, json=state.issue)

    client = GiteaClient("fake-token")
    await client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gitea.invalid/api/v1/"
    )
    try:
        thread = await open_thread(client, OWNER, REPO, 1)
        args = {
            "resolution_event_id": 7,
            "expected_state_sha256": thread["close_state_sha256"],
            "request_key": "real-http-close-operation",
            "reason": "Reviewed report, close Issue",
        }
        result = await close(client, tmp_path / "receipts", args)
        assert result["closed"] and result["reconciled"] and len(patches) == 1
        assert (await close(client, tmp_path / "receipts", args))["already_closed"]
        assert len(patches) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["caller", "permission", "repo", "audit", "token"])
async def test_adapter_authorization_and_failure_redaction(monkeypatch, tmp_path, failure):
    from examples.mcp_server.mcp_infra.adapters import coordination_adapter as adapter

    client = Client()
    args = await request(client)
    monkeypatch.setenv("GITEA_TOKEN", "sensitive-token-must-stay-private")
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(adapter, "_registered", lambda p: REPO)
    monkeypatch.setattr(adapter, "_principal", lambda: None if failure == "caller" else ACTOR)
    monkeypatch.setattr(adapter, "server_attr", lambda name: lambda _: client)

    class Audit:
        def append_required(self, event):
            if failure == "audit":
                raise OSError("sensitive-token-must-stay-private")

        def append(self, event):
            pass

    monkeypatch.setattr(adapter.remote, "_get_gitea_audit_logger", lambda: Audit())
    if failure == "permission":
        client.repo["permissions"]["push"] = False
    elif failure == "repo":
        client.repo["full_name"] = "foreign/other"
    elif failure == "token":

        async def broken(*args):
            raise RuntimeError("sensitive-token-must-stay-private")

        client.get_repo = broken
    result = await adapter.coord_close(REPO, 1, **args)
    assert result["ok"] is False and "sensitive-token-must-stay-private" not in json.dumps(result)
    assert client.patch_calls == 0


@pytest.mark.asyncio
async def test_adapter_happy_path_and_registered_mcp_schema(monkeypatch, tmp_path):
    from examples.mcp_server.mcp_infra.adapters import coordination_adapter as adapter

    client = Client()
    args = await request(client)
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(adapter, "_registered", lambda p: REPO)
    monkeypatch.setattr(adapter, "_principal", lambda: ACTOR)
    monkeypatch.setattr(adapter, "server_attr", lambda name: lambda _: client)

    class Audit:
        def append_required(self, event):
            assert event.metadata["resolution_event_id"] == 7

        def append(self, event):
            pass

    monkeypatch.setattr(adapter.remote, "_get_gitea_audit_logger", lambda: Audit())
    result = await adapter.coord_close(REPO, 1, **args)
    assert result["ok"] and result["result"]["closed"] and client.patch_calls == 1
    registered = {}

    def register(name):
        def decorator(fn):
            registered[name] = fn
            return fn

        return decorator

    monkeypatch.setattr(adapter, "register_tool", register)
    adapter.register_all()
    assert registered["coord_close"] is adapter.coord_close
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("coord-close-test")
    mcp.tool()(adapter.coord_close)
    from mcp.server.fastmcp.exceptions import ToolError

    for field in ("issue_number", "resolution_event_id"):
        for bad in (True, "1", 1.0):
            invalid = {"project": REPO, "issue_number": 1, **args, field: bad}
            with pytest.raises(ToolError, match="(?i)validation"):
                await mcp.call_tool("coord_close", invalid)
    assert client.patch_calls == 1
    tools = await mcp.list_tools()
    schema = next(t for t in tools if t.name == "coord_close").inputSchema
    assert set(schema["required"]) == {
        "project",
        "issue_number",
        "resolution_event_id",
        "expected_state_sha256",
        "request_key",
        "reason",
    }
