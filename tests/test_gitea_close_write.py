"""Security and contract tests for the narrow Gitea PR close tool.

The close tool must never touch merge or branch deletion: the only
mutation it may issue is a single PATCH that sets state=closed (see
`_patched endpoint allowlist` in gitea_client.py); head-SHA equality is
checked against a freshly-fetched PR BEFORE any mutation, so any
mismatch fails closed with zero writes.

A real close must additionally be justified by a ``superseding_ref``:
    normally a ``refs/heads/<branch>`` or plain feature branch in the SAME
    repository that the adapter freshly resolves and whose exact head commit
    equals ``expected_head_sha`` before any intent audit or close mutation.
    The sole default-branch exception is a Gitea-degenerate zero-delta PR:
    head ref == base ref == repository default branch and both SHAs equal the
    exact expected head. Missing/malformed/moved/ambiguous evidence fails
    closed with zero audit writes and zero mutation. It also requires a
    human-readable reason and strict attributed audit attribution: the Gitea username and
    the authenticated caller fingerprint must both resolve, a
    destructive-intent audit event must persist (fail closed with
    AUDIT_UNAVAILABLE otherwise), and only then may the close mutation run.
    An already-closed PR is an idempotent success with no intent audit and
    no superseding proof because no mutation occurs. The pre-mutation audit
    must be fsync-persisted before the caller proceeds; an fsync failure
    also fails closed with AUDIT_UNAVAILABLE and zero mutation.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_audit import AuditWriteError, McpAuditEvent
from examples.mcp_server.mcp_infra.adapters import remote

SHA = "a" * 40
REASON = "superseded by newer design"
FINGERPRINT = "ab" * 32


class RecordingAuditLogger:
    """Records append_required (strict) vs append (best-effort) audit events."""

    def __init__(
        self,
        order_log: list[str] | None = None,
        *,
        intent_error: Exception | None = None,
        success_error: Exception | None = None,
    ):
        self.order_log = order_log
        self.intent_error = intent_error
        self.success_error = success_error
        self.required_events: list[McpAuditEvent] = []
        self.append_events: list[McpAuditEvent] = []

    def append_required(self, event: McpAuditEvent) -> None:
        self.required_events.append(event)
        if self.order_log is not None:
            self.order_log.append("audit:intent")
        if self.intent_error is not None:
            raise self.intent_error

    def append(self, event: McpAuditEvent) -> None:
        self.append_events.append(event)
        if self.order_log is not None:
            self.order_log.append("audit:success")
        if self.success_error is not None:
            raise self.success_error


class FakeCloseClient:
    """Stand-in Gitea client: the ONLY mutation surface is close_pull_request.

    There is deliberately no merge_pull_request or branch-deletion method
    here -- any adapter attempt to merge or delete would raise
    AttributeError and surface as an INTERNAL_ERROR instead of mutating.
    """

    def __init__(
        self,
        token: str,
        *,
        state: str = "open",
        head_sha: str = SHA,
        merged: bool = False,
        post_state: str = "closed",
        post_head_sha: str = SHA,
        post_base: str = "master",
        post_merged: bool = False,
        user_payload: dict | None = None,
        superseding_head_sha: str | None = None,
        superseding_missing: bool = False,
        head_ref: str = "feat/x",
        base_sha: str = SHA,
        default_branch: str = "master",
    ):
        assert token == "token"
        self.state = state
        self.head_sha = head_sha
        self.merged = merged
        self.post_state = post_state
        self.post_head_sha = post_head_sha
        self.post_base = post_base
        self.post_merged = post_merged
        self.user_payload = user_payload
        self.superseding_head_sha = (
            superseding_head_sha if superseding_head_sha is not None else SHA
        )
        self.superseding_missing = superseding_missing
        self.head_ref = head_ref
        self.base_sha = base_sha
        self.default_branch = default_branch
        self.pr_reads = 0
        self.branch_lookups: list[str] = []
        self.close_calls: list[tuple[str, str, int]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def _payload(
        self, pull_number: int, *, state: str, head_sha: str, merged: bool, base: str
    ) -> dict:
        return {
            "number": pull_number,
            "state": state,
            "merged": merged,
            "head": {"sha": head_sha, "ref": self.head_ref},
            "base": {"ref": base, "sha": self.base_sha},
            "html_url": "https://git.example/pr/25",
        }

    async def get_user(self):
        payload = self.user_payload
        if payload is None:
            payload = {"login": "robot"}
        return payload

    async def get_repo(self, owner: str, repo: str):
        return {"default_branch": self.default_branch}

    async def get_branch(self, owner: str, repo: str, branch: str):
        if self.superseding_missing:
            request = httpx.Request(
                "GET",
                f"https://git.example.invalid/api/v1/repos/{owner}/{repo}/branches/{branch}",
            )
            raise httpx.HTTPStatusError(
                f"gitea api /repos/{owner}/{repo}/branches/{branch}: 404 Not Found",
                request=request,
                response=httpx.Response(404, request=request),
            )
        self.branch_lookups.append(branch)
        return {
            "name": branch,
            "commit": {"id": self.superseding_head_sha},
            "protected": False,
        }

    async def get_pull_request(self, owner: str, repo: str, pull_number: int):
        self.pr_reads += 1
        if self.pr_reads == 1:
            return self._payload(
                pull_number,
                state=self.state,
                head_sha=self.head_sha,
                merged=self.merged,
                base="master",
            )
        return self._payload(
            pull_number,
            state=self.post_state,
            head_sha=self.post_head_sha,
            merged=self.post_merged,
            base=self.post_base,
        )

    async def close_pull_request(self, owner: str, repo: str, pull_number: int):
        self.close_calls.append((owner, repo, pull_number))
        return {"number": pull_number, "state": "closed"}


def _setup_close(
    monkeypatch,
    client: FakeCloseClient,
    *,
    fingerprint: str | None = FINGERPRINT,
    audit_logger: RecordingAuditLogger | None = None,
) -> RecordingAuditLogger:
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    if audit_logger is None:
        audit_logger = RecordingAuditLogger()
    logger = audit_logger

    def fake_server_attr(name: str):
        if name == "_current_auth_reuse_key":
            return lambda: fingerprint
        if name == "get_audit_logger":
            return lambda: logger
        raise AssertionError(f"unexpected server_attr({name!r})")

    monkeypatch.setattr(remote, "server_attr", fake_server_attr)
    return logger


async def _close(
    monkeypatch,
    client: FakeCloseClient,
    *,
    reason: str | None = REASON,
    superseding_ref: str | None = "refs/heads/new-design",
    fingerprint: str | None = FINGERPRINT,
    audit_logger: RecordingAuditLogger | None = None,
) -> dict:
    call_reason = REASON if reason is None else reason
    _setup_close(monkeypatch, client, fingerprint=fingerprint, audit_logger=audit_logger)
    result = await remote.gitea_close_pull_request(
        "owner",
        "repo",
        25,
        SHA,
        call_reason,
        superseding_ref=superseding_ref,
    )
    return result


# ---------------------------------------------------------------------------
# GiteaClient-level endpoint contract (unchanged behavior)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_close_pr_uses_fixed_endpoint_and_state_closed(monkeypatch):
    client = GiteaClient("token")
    patch = AsyncMock(return_value={"number": 25, "state": "closed"})
    monkeypatch.setattr(client, "_patch", patch)
    try:
        result = await client.close_pull_request("owner", "repo", 25)
    finally:
        await client.aclose()

    assert result == {"number": 25, "state": "closed"}
    patch.assert_awaited_once_with(
        "/repos/{owner}/{repo}/pulls/{number}",
        {"state": "closed"},
        owner="owner",
        repo="repo",
        number=25,
    )


@pytest.mark.asyncio
async def test_client_close_pr_rejects_bad_pull_number():
    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="pull_number"):
            await client.close_pull_request("owner", "repo", 0)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_close_patch_rejects_non_allowlisted_endpoints():
    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="Write endpoint not allowed"):
            await client._patch(
                "/repos/{owner}/{repo}/issues/{number}",
                {"state": "closed"},
                owner="owner",
                repo="repo",
                number=25,
            )
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Reason / superseding_ref validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_requires_reason(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, "", superseding_ref="refs/heads/new-design"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "reason" in result["error"]["message"]


@pytest.mark.asyncio
async def test_adapter_requires_nonempty_reason_after_strip(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, "   ", superseding_ref="refs/heads/new-design"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "reason" in result["error"]["message"]


@pytest.mark.asyncio
async def test_adapter_rejects_reason_longer_than_500(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, "x" * 501, superseding_ref="refs/heads/new-design"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "500" in result["error"]["message"]


@pytest.mark.asyncio
async def test_adapter_rejects_reason_with_ascii_control(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, "bad\x00reason", superseding_ref="refs/heads/new-design"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "control" in result["error"]["message"]


@pytest.mark.asyncio
async def test_adapter_rejects_superseding_ref_longer_than_255(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, REASON, superseding_ref="r" * 256
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "255" in result["error"]["message"]


@pytest.mark.asyncio
async def test_adapter_rejects_superseding_ref_with_ascii_control(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, REASON, superseding_ref="ref\nx"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "control" in result["error"]["message"]


@pytest.mark.asyncio
async def test_adapter_rejects_invalid_head_sha(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, "abc", REASON, superseding_ref="refs/heads/new-design"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_adapter_requires_gitea_token(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, REASON, superseding_ref="refs/heads/new-design"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPENDENCY_MISSING"


# ---------------------------------------------------------------------------
# Identity / fingerprint attribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_missing_caller_fingerprint_fails_auth_zero_mutation(monkeypatch):
    client = FakeCloseClient("token")

    result = await _close(monkeypatch, client, fingerprint=None)

    assert result["ok"] is False
    assert result["error"]["code"] == "AUTH_ERROR"
    assert client.close_calls == []


@pytest.mark.asyncio
async def test_adapter_missing_gitea_username_fails_auth_zero_mutation(monkeypatch):
    client = FakeCloseClient("token", user_payload={"login": "   "})
    logger = RecordingAuditLogger()

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "AUTH_ERROR"
    assert client.close_calls == []
    assert logger.required_events == []


# ---------------------------------------------------------------------------
# Audit failure: fail closed with AUDIT_UNAVAILABLE, zero mutation, no leakage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_intent_audit_failure_returns_audit_unavailable_zero_mutation(
    monkeypatch,
):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger(
        intent_error=AuditWriteError("/var/lib/nonexistent/audit.jsonl: read-only file system")
    )

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert result["error"]["message"] == "Audit log unavailable; destructive operation refused"
    assert client.close_calls == []
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_audit_failure_does_not_leak_internal_details(monkeypatch):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger(
        intent_error=AuditWriteError("/srv/audit.jsonl on /dev/sda1: disk full")
    )

    result = await _close(monkeypatch, client, audit_logger=logger)
    message = result["error"]["message"]
    assert "srv" not in message
    assert "sda1" not in message
    assert "disk full" not in message
    assert "/" not in message.replace(" ", "").replace(";", "")


@pytest.mark.asyncio
async def test_adapter_fsync_failure_is_audit_unavailable_zero_mutation(
    monkeypatch, tmp_path
):
    """A real McpAuditLogger whose os.fsync() fails must raise AuditWriteError
    before any mutation: AUDIT_UNAVAILABLE, zero close calls, untouched buffer."""
    from examples.mcp_server.mcp_audit import McpAuditLogger

    client = FakeCloseClient("token")
    logger = McpAuditLogger(log_path=str(tmp_path / "audit.jsonl"))
    _setup_close(monkeypatch, client, audit_logger=logger)

    def boom(fd):
        raise OSError("fsync failed")

    monkeypatch.setattr("examples.mcp_server.mcp_audit.os.fsync", boom)

    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, REASON, superseding_ref="refs/heads/new-design"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert client.close_calls == []
    assert logger._buffer == []


# ---------------------------------------------------------------------------
# Successful close: mutation, readback, ordering, audit metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_closes_exact_open_pr_once(monkeypatch):
    client = FakeCloseClient("token")
    _setup_close(monkeypatch, client)

    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, REASON, superseding_ref="refs/heads/new-design"
    )

    assert result["ok"] is True
    assert client.pr_reads == 2
    assert client.branch_lookups == ["new-design"]
    assert client.close_calls == [("owner", "repo", 25)]
    assert result["result"] == {
        "number": 25,
        "closed": True,
        "already_closed": False,
        "merged": False,
        "head_sha": SHA,
        "base": "master",
        "html_url": "https://git.example/pr/25",
        "verified": True,
    }


@pytest.mark.asyncio
async def test_adapter_strips_reason_before_audit_and_mutation(monkeypatch):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger()
    _setup_close(monkeypatch, client, audit_logger=logger)

    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, "  cleanup work  ", superseding_ref="refs/heads/new-design"
    )

    assert result["ok"] is True
    assert logger.required_events[0].metadata["reason"] == "cleanup work"


@pytest.mark.asyncio
async def test_adapter_superseding_ref_recorded_in_audit_with_verified_head(monkeypatch):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger()
    _setup_close(monkeypatch, client, audit_logger=logger)

    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, REASON, superseding_ref="refs/heads/new-design"
    )

    assert result["ok"] is True
    assert client.branch_lookups == ["new-design"]
    for event in (logger.required_events[0], logger.append_events[0]):
        assert event.metadata["superseding_ref"] == "refs/heads/new-design"
        assert event.metadata["superseding_head_sha"] == SHA


@pytest.mark.asyncio
async def test_adapter_plain_superseding_branch_normalized_in_audit(monkeypatch):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger()
    _setup_close(monkeypatch, client, audit_logger=logger)

    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, REASON, superseding_ref="new-design"
    )

    assert result["ok"] is True
    assert client.branch_lookups == ["new-design"]
    for event in (logger.required_events[0], logger.append_events[0]):
        assert event.metadata["superseding_ref"] == "refs/heads/new-design"
        assert event.metadata["superseding_head_sha"] == SHA


@pytest.mark.asyncio
async def test_adapter_allows_default_branch_only_for_degenerate_same_ref_pr(monkeypatch):
    client = FakeCloseClient("token", head_ref="master")
    logger = RecordingAuditLogger()
    _setup_close(monkeypatch, client, audit_logger=logger)

    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, REASON, superseding_ref="master"
    )

    assert result["ok"] is True
    assert client.branch_lookups == ["master"]
    assert client.close_calls == [("owner", "repo", 25)]
    for event in (logger.required_events[0], logger.append_events[0]):
        assert event.metadata["superseding_ref"] == "refs/heads/master"
        assert event.metadata["superseding_head_sha"] == SHA
        assert event.metadata["degenerate_same_default_ref"] is True


@pytest.mark.asyncio
async def test_adapter_degenerate_default_ref_requires_exact_base_sha(monkeypatch):
    client = FakeCloseClient("token", head_ref="master", base_sha="c" * 40)
    logger = RecordingAuditLogger()

    result = await _close(monkeypatch, client, superseding_ref="master", audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert client.branch_lookups == []
    assert client.close_calls == []
    assert logger.required_events == []
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_open_close_without_superseding_ref_is_policy_denied(monkeypatch):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger()

    result = await _close(monkeypatch, client, superseding_ref=None, audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "superseding_ref" in result["error"]["message"]
    assert client.branch_lookups == []
    assert client.close_calls == []
    assert logger.required_events == []
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_open_close_with_empty_superseding_ref_is_policy_denied(monkeypatch):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger()

    result = await _close(monkeypatch, client, superseding_ref="", audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "superseding_ref" in result["error"]["message"]
    assert client.branch_lookups == []
    assert client.close_calls == []
    assert logger.required_events == []
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_missing_superseding_branch_fails_closed_zero_mutation(monkeypatch):
    client = FakeCloseClient("token", superseding_missing=True)
    logger = RecordingAuditLogger()

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "does not exist" in result["error"]["message"]
    assert client.close_calls == []
    assert logger.required_events == []
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_wrong_superseding_head_fails_closed_zero_mutation(monkeypatch):
    client = FakeCloseClient("token", superseding_head_sha="c" * 40)
    logger = RecordingAuditLogger()

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "does not equal" in result["error"]["message"]
    assert result["error"]["details"]["observed_superseding_head_sha"] == "c" * 40
    assert client.close_calls == []
    assert logger.required_events == []
    assert logger.append_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "superseding_ref",
    [
        "refs/tags/v1.0",
        "refs/heads/../escape",
        "refs/heads/",
        "main",
        "master",
        "-leading-dash",
        "has space",
    ],
)
async def test_adapter_malformed_superseding_ref_fails_closed_zero_mutation(
    monkeypatch, superseding_ref
):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger()

    result = await _close(monkeypatch, client, superseding_ref=superseding_ref, audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert client.branch_lookups == []
    assert client.close_calls == []
    assert logger.required_events == []
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_audit_attribution_metadata_and_correlation(monkeypatch):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger()
    _setup_close(monkeypatch, client, audit_logger=logger)

    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, REASON, superseding_ref="refs/heads/new-design"
    )

    assert result["ok"] is True
    assert len(logger.required_events) == 1
    assert len(logger.append_events) == 1
    intent = logger.required_events[0]
    success = logger.append_events[0]
    assert intent.event_type == "mcp.gitea_destructive_intent"
    assert intent.decision == "allow"
    assert intent.tool == "gitea_close_pull_request"
    assert success.event_type == "mcp.gitea_destructive_success"
    assert success.decision == "allow"
    correlation_id = intent.metadata["correlation_id"]
    assert success.metadata["correlation_id"] == correlation_id
    assert re.fullmatch(r"[0-9a-f]{32}", correlation_id), correlation_id
    for event in (intent, success):
        assert event.metadata["owner"] == "owner"
        assert event.metadata["repo"] == "repo"
        assert event.metadata["pull_number"] == 25
        assert event.metadata["expected_head_sha"] == SHA
        assert event.metadata["reason"] == REASON
        assert event.metadata["superseding_ref"] == "refs/heads/new-design"
        assert event.metadata["superseding_head_sha"] == SHA
        assert event.metadata["gitea_username"] == "robot"
        assert event.metadata["caller_fingerprint"] == FINGERPRINT
        assert re.fullmatch(r"[0-9a-f]{64}", event.metadata["caller_fingerprint"])
        assert "token" not in event.metadata


@pytest.mark.asyncio
async def test_adapter_audit_events_contain_no_credentials(monkeypatch):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger()
    _setup_close(monkeypatch, client, audit_logger=logger)

    result = await remote.gitea_close_pull_request(
        "owner", "repo", 25, SHA, REASON, superseding_ref="refs/heads/new-design"
    )

    assert result["ok"] is True
    for event in (*logger.required_events, *logger.append_events):
        dump = repr(event)
        assert "super-secret" not in dump
        assert "GITEA_TOKEN" not in dump
        assert "Bearer" not in dump


@pytest.mark.asyncio
async def test_adapter_audit_order_intent_before_mutation_after(monkeypatch):
    order: list[str] = []
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger(order_log=order)
    _setup_close(monkeypatch, client, audit_logger=logger)

    original_close = client.close_pull_request

    async def record_close(owner: str, repo: str, pull_number: int):
        order.append("close")
        return await original_close(owner, repo, pull_number)

    client.close_pull_request = record_close  # type: ignore[method-assign]

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is True
    assert order == ["audit:intent", "close", "audit:success"]


@pytest.mark.asyncio
async def test_adapter_success_audit_is_best_effort(monkeypatch):
    client = FakeCloseClient("token")
    logger = RecordingAuditLogger(success_error=RuntimeError("disk full"))
    _setup_close(monkeypatch, client, audit_logger=logger)

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is True
    assert client.close_calls == [("owner", "repo", 25)]


# ---------------------------------------------------------------------------
# Head/base/merged readback invariants (must be preserved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_head_mismatch_fails_closed_zero_mutation(monkeypatch):
    client = FakeCloseClient("token", head_sha="c" * 40)
    logger = RecordingAuditLogger()

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert client.close_calls == []
    assert logger.required_events == []
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_already_closed_same_sha_is_idempotent(monkeypatch):
    client = FakeCloseClient("token", state="closed")
    logger = RecordingAuditLogger()

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is True
    assert result["result"]["number"] == 25
    assert result["result"]["closed"] is True
    assert result["result"]["already_closed"] is True
    assert result["result"]["merged"] is False
    assert result["result"]["head_sha"] == SHA
    assert result["result"]["base"] == "master"
    assert result["result"]["html_url"] == "https://git.example/pr/25"
    assert result["result"]["verified"] is True
    assert client.pr_reads == 2
    assert client.branch_lookups == []
    assert client.close_calls == []
    assert logger.required_events == []
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_already_closed_emits_no_intent_audit(monkeypatch):
    client = FakeCloseClient("token", state="closed")
    logger = RecordingAuditLogger()

    result = await _close(monkeypatch, client, fingerprint=None, audit_logger=logger)

    assert result["ok"] is True
    assert result["result"]["already_closed"] is True
    assert logger.required_events == []
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_already_closed_wrong_sha_fails_closed(monkeypatch):
    client = FakeCloseClient("token", state="closed", head_sha="c" * 40)
    _setup_close(monkeypatch, client)

    result = await _close(monkeypatch, client)

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert client.close_calls == []


@pytest.mark.asyncio
async def test_adapter_surfaces_close_api_failure(monkeypatch):
    client = FakeCloseClient("token")

    async def boom(owner: str, repo: str, pull_number: int):
        raise PermissionError("gitea api /repos/o/r/pulls/25: forbidden")

    client.close_pull_request = boom  # type: ignore[method-assign]
    _setup_close(monkeypatch, client)

    result = await _close(monkeypatch, client)

    assert result["ok"] is False
    assert result["error"]["code"] == "AUTH_ERROR"


@pytest.mark.asyncio
async def test_adapter_post_close_readback_confirms_merged_false_and_exact_state(monkeypatch):
    """A fresh post-close GET must confirm merged=false and preserved head/base."""
    client = FakeCloseClient("token")
    _setup_close(monkeypatch, client)

    result = await _close(monkeypatch, client)

    assert result["ok"] is True
    assert client.pr_reads == 2
    assert result["result"]["closed"] is True
    assert result["result"]["already_closed"] is False
    assert result["result"]["merged"] is False
    assert result["result"]["head_sha"] == SHA
    assert result["result"]["base"] == "master"
    assert result["result"]["verified"] is True


@pytest.mark.asyncio
async def test_adapter_fails_closed_when_readback_shows_merged_true(monkeypatch):
    """Post-close readback reporting merged=true must not be reported as success."""
    client = FakeCloseClient("token", post_merged=True)
    logger = _setup_close(monkeypatch, client)

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "CLOSE_NOT_CONFIRMED"
    assert "merged" in result["error"]["message"]
    assert client.close_calls == [("owner", "repo", 25)]
    assert len(logger.required_events) == 1
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_fails_closed_when_readback_head_drifted(monkeypatch):
    """Post-close head must exactly equal the expected SHA."""
    client = FakeCloseClient("token", post_head_sha="c" * 40)
    logger = _setup_close(monkeypatch, client)

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "CLOSE_NOT_CONFIRMED"
    assert "head" in result["error"]["message"]
    assert client.close_calls == [("owner", "repo", 25)]
    assert len(logger.required_events) == 1
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_fails_closed_when_readback_base_drifted(monkeypatch):
    """Post-close base ref must exactly equal the pre-close base."""
    client = FakeCloseClient("token", post_base="main")
    logger = _setup_close(monkeypatch, client)

    result = await _close(monkeypatch, client, audit_logger=logger)

    assert result["ok"] is False
    assert result["error"]["code"] == "CLOSE_NOT_CONFIRMED"
    assert "base" in result["error"]["message"]
    assert client.close_calls == [("owner", "repo", 25)]
    assert len(logger.required_events) == 1
    assert logger.append_events == []


@pytest.mark.asyncio
async def test_adapter_idempotent_reclose_verifies_merged_false(monkeypatch):
    """Already-closed PR cleanup still verifies the merged=false invariant."""
    client = FakeCloseClient("token", state="closed")
    _setup_close(monkeypatch, client)

    result = await _close(monkeypatch, client)

    assert result["ok"] is True
    assert result["result"]["already_closed"] is True
    assert result["result"]["merged"] is False
    assert result["result"]["verified"] is True
    assert client.pr_reads == 2
    assert client.close_calls == []