"""In-flight exact-SHA Gitea status reads must not multiply rate-limit pressure."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from examples.mcp_server.mcp_infra.adapters import remote

SHA = "a" * 40
OTHER_SHA = "b" * 40


@pytest.fixture(autouse=True)
def _bound_caller(monkeypatch):
    monkeypatch.setattr(remote, "_caller_fingerprint", lambda: "caller-a")


class BlockingStatusClient:
    def __init__(self, token: str, calls: list[tuple], started: asyncio.Event, release: asyncio.Event):
        self.token = token
        self.calls = calls
        self.started = started
        self.release = release

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def list_commit_statuses(self, owner, repo, sha, *, page, limit):
        self.calls.append((self.token, owner, repo, sha, page, limit))
        self.started.set()
        await self.release.wait()
        return [{"context": "ci/verify", "state": "success"}]


@pytest.mark.asyncio
async def test_concurrent_identical_exact_sha_reads_share_one_upstream_and_not_responses(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "auth-a")
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[tuple] = []
    monkeypatch.setattr(
        remote, "_server_gitea_client",
        lambda: lambda token: BlockingStatusClient(token, calls, started, release),
    )
    first = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    await asyncio.wait_for(started.wait(), 3)
    second = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    try:
        await asyncio.sleep(0)
    finally:
        release.set()
    a, b = await asyncio.gather(first, second)
    assert len(calls) == 1
    assert a["ok"] and b["ok"]
    assert a["meta"]["request_id"] != b["meta"]["request_id"]
    assert a["result"]["statuses"] is not b["result"]["statuses"]
    a["result"]["statuses"][0]["context"] = "caller-only"
    assert b["result"]["statuses"][0]["context"] == "ci/verify"
    assert (await remote.gitea_list_commit_statuses("owner", "repo", SHA))["ok"]
    assert len(calls) == 2  # Only in-flight coalescing; no stale cache.


@pytest.mark.asyncio
@pytest.mark.parametrize("different", ["sha", "page", "repo", "token", "caller"])
async def test_exact_status_reads_never_cross_key_or_authority(monkeypatch, different):
    monkeypatch.setenv("GITEA_TOKEN", "auth-a")
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[tuple] = []
    monkeypatch.setattr(
        remote, "_server_gitea_client",
        lambda: lambda token: BlockingStatusClient(token, calls, started, release),
    )
    first = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    await asyncio.wait_for(started.wait(), 3)
    kwargs = {"owner": "owner", "repo": "repo", "sha": SHA, "page": 1}
    if different == "sha":
        kwargs["sha"] = OTHER_SHA
    elif different == "page":
        kwargs["page"] = 2
    elif different == "repo":
        kwargs["repo"] = "another"
    elif different == "token":
        monkeypatch.setenv("GITEA_TOKEN", "auth-b")
    else:
        monkeypatch.setattr(remote, "_caller_fingerprint", lambda: "caller-b")
    second = asyncio.create_task(remote.gitea_list_commit_statuses(**kwargs))
    try:
        for _ in range(40):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == 2
    finally:
        release.set()
    assert all(r["ok"] for r in await asyncio.gather(first, second))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cancelled_status_caller_does_not_cancel_other_waiter(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "auth-a")
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[tuple] = []
    monkeypatch.setattr(
        remote, "_server_gitea_client",
        lambda: lambda token: BlockingStatusClient(token, calls, started, release),
    )
    first = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    await asyncio.wait_for(started.wait(), 3)
    other = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    await asyncio.sleep(0)
    first.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
    assert (await asyncio.wait_for(other, 3))["ok"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_shared_malformed_response_fails_closed_and_next_read_refetches(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "auth-a")
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[int] = []

    class InvalidOnce:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def list_commit_statuses(self, *_args, **_kwargs):
            calls.append(1)
            started.set()
            await release.wait()
            if len(calls) == 1:
                raise remote.GiteaCommitStatusResponseError("malformed status page")
            return []

    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: InvalidOnce())
    first = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    await asyncio.wait_for(started.wait(), 3)
    second = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    try:
        await asyncio.sleep(0)
    finally:
        release.set()
    a, b = await asyncio.gather(first, second)
    assert len(calls) == 1
    assert a["error"]["code"] == b["error"]["code"] == "REMOTE_RESPONSE_INVALID"
    recovered = await remote.gitea_list_commit_statuses("owner", "repo", SHA)
    assert recovered["ok"] and recovered["result"]["absent"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_capacity_overflow_preserves_independent_direct_read(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "auth-a")
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[tuple] = []
    monkeypatch.setattr(remote, "_STATUS_READ_MAX_INFLIGHT", 1)
    monkeypatch.setattr(
        remote, "_server_gitea_client",
        lambda: lambda token: BlockingStatusClient(token, calls, started, release),
    )
    first = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    await asyncio.wait_for(started.wait(), 3)
    other = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", OTHER_SHA))
    try:
        for _ in range(40):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == 2
    finally:
        release.set()
    assert all(r["ok"] for r in await asyncio.gather(first, other))
    assert not remote._STATUS_READ_INFLIGHT


@pytest.mark.asyncio
async def test_cross_unknown_caller_identity_falls_back_to_separate_reads(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "auth-a")
    monkeypatch.setattr(remote, "_caller_fingerprint", lambda: None)
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[tuple] = []
    monkeypatch.setattr(
        remote, "_server_gitea_client",
        lambda: lambda token: BlockingStatusClient(token, calls, started, release),
    )
    first = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    await asyncio.wait_for(started.wait(), 3)
    second = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    try:
        for _ in range(40):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == 2
    finally:
        release.set()
    assert all(r["ok"] for r in await asyncio.gather(first, second))


@pytest.mark.asyncio
async def test_concurrent_rate_limited_statuses_preserve_retry_metadata(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "auth-a")
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[int] = []

    class LimitedClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def list_commit_statuses(self, *_args, **_kwargs):
            calls.append(1)
            started.set()
            await release.wait()
            request = httpx.Request(
                "GET", "https://git.example.test/api/v1/repos/owner/repo/statuses/" + SHA
            )
            response = httpx.Response(429, request=request, headers={"Retry-After": "11"})
            raise httpx.HTTPStatusError(
                "Gitea rate limited", request=request, response=response
            )

    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: LimitedClient())
    first = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    await asyncio.wait_for(started.wait(), 3)
    second = asyncio.create_task(remote.gitea_list_commit_statuses("owner", "repo", SHA))
    try:
        await asyncio.sleep(0)
    finally:
        release.set()
    a, b = await asyncio.gather(first, second)
    assert len(calls) == 1
    for result in (a, b):
        assert result["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert result["error"]["details"] == {
            "bucket_class": "remote_api",
            "operation_class": "read",
            "retry_after_seconds": 11,
        }
        assert result["error"]["retryable"] is True
    assert a["meta"]["request_id"] != b["meta"]["request_id"]
