"""Guarded Gitea Issue reopen: close-only lease, auth, audit, race and ambiguity."""

from __future__ import annotations

import pytest

from examples.mcp_server.mcp_audit import AuditWriteError
from examples.mcp_server.mcp_infra.adapters import remote

UPDATED = "2026-11-17T18:30:00+03:00"
REASON = "The automatic PR close was premature; production release remains unverified."
FINGERPRINT = "9a" * 32


class FakeClient:
    def __init__(
        self, *, state="closed", updated=UPDATED, pr=False, apply=True,
        second_drift=None, raise_after_patch=False, deny=False, bad_identity=False,
    ):
        self.state = state
        self.updated = updated
        self.pr = pr
        self.apply = apply
        self.second_drift = second_drift
        self.raise_after_patch = raise_after_patch
        self.deny = deny
        self.bad_identity = bad_identity
        self.reads = 0
        self.patches = 0
        self.order = []
        self.body = "Awaiting production smoke"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_issue(self, owner, repo, number):
        self.order.append("read")
        self.reads += 1
        if self.reads == 2:
            if self.second_drift == "timestamp":
                self.updated = "2026-11-17T18:31:00+03:00"
            elif self.second_drift == "state":
                self.state = "open"
            elif self.second_drift == "body":
                self.body = "Modified without updated_at"
            elif self.second_drift == "pr":
                self.pr = True
        return {
            "number": number, "state": self.state, "updated_at": self.updated,
            "title": "Release not accepted", "body": self.body, "labels": [],
            "html_url": "https://git.example/owner/repo/issues/43",
            "pull_request": {"merged": False} if self.pr else None,
        }

    async def get_user(self):
        self.order.append("identity")
        return {"login": "" if self.bad_identity else "operator"}

    async def reopen_coord_issue(self, owner, repo, number):
        self.order.append("patch")
        self.patches += 1
        if self.deny:
            raise PermissionError("forbidden")
        if self.apply:
            self.state = "open"
            self.updated = "2026-11-17T18:32:00+03:00"
        if self.raise_after_patch:
            raise OSError("transport died after PATCH")
        return {"number": number, "state": self.state}


class FakeAudit:
    def __init__(self, order, *, fail=False):
        self.order = order
        self.fail = fail
        self.intents = []
        self.successes = []

    def append_required(self, event):
        self.order.append("audit:intent")
        self.intents.append(event)
        if self.fail:
            raise AuditWriteError("secret path or bearer token must not appear")

    def append(self, event):
        self.order.append("audit:success")
        self.successes.append(event)


def setup(monkeypatch, client, *, fail_audit=False, fingerprint=FINGERPRINT):
    monkeypatch.setenv("GITEA_TOKEN", "private-test-token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    monkeypatch.setattr(remote, "_caller_fingerprint", lambda: fingerprint)
    audit = FakeAudit(client.order, fail=fail_audit)
    monkeypatch.setattr(remote, "_get_gitea_audit_logger", lambda: audit)
    return audit


async def reopen(*, updated=UPDATED, reason=REASON, number=43):
    return await remote.gitea_reopen_issue(
        "owner", "repo", number, expected_updated_at=updated, reason=reason,
    )


@pytest.mark.asyncio
async def test_reopen_verified_exact_single_patch_and_audited(monkeypatch):
    client = FakeClient()
    audit = setup(monkeypatch, client)
    result = await reopen()
    assert result["ok"] is True
    assert result["result"]["open"] is True
    assert result["result"]["already_open"] is False
    assert result["result"]["observed_updated_at"] == client.updated
    assert client.order == [
        "read", "identity", "read", "audit:intent", "patch", "read",
        "audit:success",
    ]
    assert client.patches == 1
    assert len(audit.intents) == len(audit.successes) == 1
    assert audit.intents[0].action == "reopen_issue"
    assert audit.intents[0].reason == REASON
    assert audit.intents[0].metadata["before_state"] == "closed"
    assert audit.intents[0].metadata["target_state"] == "open"
    assert audit.successes[0].metadata["observed_state"] == "open"
    assert audit.intents[0].metadata["caller_fingerprint"] == FINGERPRINT


@pytest.mark.asyncio
async def test_already_open_is_verified_noop_even_if_updated_at_differs(monkeypatch):
    client = FakeClient(state="open", updated="2026-11-17T19:00:00+03:00")
    audit = setup(monkeypatch, client)
    result = await reopen()
    assert result["ok"] is True
    assert result["result"]["already_open"] is True
    assert result["result"]["verified"] is True
    assert client.order == ["read"]
    assert client.patches == 0 and not audit.intents


@pytest.mark.asyncio
@pytest.mark.parametrize("state,updated,pr,code", [
    ("unknown", UPDATED, False, "ISSUE_NOT_CLOSED"),
    ("closed", "2026-11-17T19:00:00+03:00", False, "STALE_ISSUE"),
    ("closed", UPDATED, True, "POLICY_DENIED"),
])
async def test_reopen_bad_state_stale_or_pr_no_write(monkeypatch, state, updated, pr, code):
    client = FakeClient(state=state, updated=updated, pr=pr)
    audit = setup(monkeypatch, client)
    result = await reopen()
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert client.patches == 0 and not audit.intents


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["timestamp", "state", "body", "pr"])
async def test_reopen_second_preflight_detects_drift(monkeypatch, drift):
    client = FakeClient(second_drift=drift)
    audit = setup(monkeypatch, client)
    result = await reopen()
    assert result["ok"] is False
    assert result["error"]["code"] == "STALE_ISSUE"
    assert client.reads == 2
    assert client.patches == 0 and not audit.intents


@pytest.mark.asyncio
async def test_missing_identity_fails_closed(monkeypatch):
    client = FakeClient()
    audit = setup(monkeypatch, client, fingerprint=None)
    result = await reopen()
    assert result["ok"] is False
    assert result["error"]["code"] == "AUTH_ERROR"
    assert client.patches == 0 and not audit.intents


@pytest.mark.asyncio
async def test_required_audit_failure_fails_closed(monkeypatch):
    client = FakeClient()
    audit = setup(monkeypatch, client, fail_audit=True)
    result = await reopen()
    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert client.patches == 0 and len(audit.intents) == 1
    assert "secret path" not in str(result)


@pytest.mark.asyncio
async def test_patch_authorization_denial_never_reports_success(monkeypatch):
    client = FakeClient(deny=True)
    audit = setup(monkeypatch, client)
    result = await reopen()
    assert result["ok"] is False
    assert client.patches == 1 and len(audit.intents) == 1
    assert not audit.successes


@pytest.mark.asyncio
@pytest.mark.parametrize("applied,observed_state", [(True, "open"), (False, "closed")])
async def test_ambiguous_timeout_reads_once_without_replaying(monkeypatch, applied, observed_state):
    client = FakeClient(apply=applied, raise_after_patch=True)
    audit = setup(monkeypatch, client)
    result = await reopen()
    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["mutation_started"] is True
    assert result["error"]["details"]["observed_state"] == observed_state
    assert client.reads == 3 and client.patches == 1
    assert len(audit.intents) == 1 and not audit.successes


@pytest.mark.asyncio
async def test_no_false_success_when_patch_did_not_change_state(monkeypatch):
    client = FakeClient(apply=False)
    setup(monkeypatch, client)
    result = await reopen()
    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert client.patches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("updated,reason,number", [
    ("not-RFC3339", REASON, 43),
    (UPDATED, "short", 43),
    (UPDATED, "Unauthorized\\nline", 43),
    (UPDATED, REASON, 0),
])
async def test_invalid_arguments_rejected_before_auth_or_remote(monkeypatch, updated, reason, number):
    client = FakeClient()
    audit = setup(monkeypatch, client)
    result = await reopen(updated=updated, reason=reason, number=number)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert not client.order and not audit.intents


@pytest.mark.asyncio
async def test_transport_uses_exact_issue_state_patch_only():
    from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

    requests = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"number": 43, "state": "open"}

    class Transport:
        async def patch(self, path, *, json):
            requests.append((path, json))
            return Response()

    client = GiteaClient("no-real-token")
    client._client = Transport()
    result = await client.reopen_coord_issue("owner", "repo", 43)
    assert result["state"] == "open"
    assert requests == [
        ("/repos/owner/repo/issues/43", {"state": "open"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_transport_permission_denial_no_fallback_or_retry(status):
    from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

    calls = []

    class Denied:
        status_code = status

    class Transport:
        async def patch(self, path, *, json):
            calls.append((path, json))
            return Denied()

    client = GiteaClient("no-real-token")
    client._client = Transport()
    with pytest.raises(PermissionError, match="not authorized"):
        await client.reopen_coord_issue("owner", "repo", 43)
    assert calls == [
        ("/repos/owner/repo/issues/43", {"state": "open"}),
    ]


@pytest.mark.asyncio
async def test_transport_malformed_success_reply_is_unknown():
    from examples.mcp_client_remote.fleet.gitea_client import (
        GiteaClient,
        GiteaMutationOutcomeUnknown,
    )

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return ["unexpected"]

    class Transport:
        async def patch(self, path, *, json):
            return Response()

    client = GiteaClient("no-real-token")
    client._client = Transport()
    with pytest.raises(GiteaMutationOutcomeUnknown):
        await client.reopen_coord_issue("owner", "repo", 43)
