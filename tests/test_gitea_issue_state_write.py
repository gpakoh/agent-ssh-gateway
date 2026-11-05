"""Fail-closed issue close: exact Gitea issue version, identity, audit and verification."""

from __future__ import annotations

import pytest

from examples.mcp_server.mcp_audit import AuditWriteError
from examples.mcp_server.mcp_infra.adapters import remote

UPDATED = "2026-11-05T10:00:00+03:00"
REASON = "Verified implementation is integrated and accepted in production."
FP = "ab" * 32


class Client:
    def __init__(
        self, *, state="open", updated=UPDATED, pr=False, apply=True,
        drift_at_second=False, raise_after_close=False,
    ):
        self.state = state
        self.updated = updated
        self.pr = pr
        self.apply = apply
        self.drift_at_second = drift_at_second
        self.raise_after_close = raise_after_close
        self.patches = 0
        self.reads = 0
        self.order = []
        self.auth = {"login": "release-owner"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_issue(self, owner, repo, number):
        self.order.append("read")
        self.reads += 1
        if self.drift_at_second and self.reads == 2:
            self.updated = "2026-11-05T10:00:05+03:00"
        result = {
            "number": number, "state": self.state,
            "updated_at": self.updated,
            "html_url": "https://git.example/owner/repo/issues/7",
            # Gitea may serialize a null pull_request for a genuine Issue.
            "pull_request": {"merged": False} if self.pr else None,
        }
        return result

    async def get_user(self):
        self.order.append("identity")
        return self.auth

    async def close_coord_issue(self, owner, repo, number):
        self.order.append("patch")
        self.patches += 1
        if self.apply:
            self.state = "closed"
            self.updated = "2026-11-05T10:01:00+03:00"
        if self.raise_after_close:
            raise OSError("write outcome lost after response boundary")
        return {"number": number, "state": self.state}


class Audit:
    def __init__(self, order, *, failed=False):
        self.order = order
        self.failed = failed
        self.intents = []
        self.successes = []

    def append_required(self, event):
        self.order.append("audit:intent")
        self.intents.append(event)
        if self.failed:
            raise AuditWriteError("private /path secret auth=token")

    def append(self, event):
        self.order.append("audit:success")
        self.successes.append(event)


def configure(monkeypatch, client, *, failed=False, fingerprint=FP):
    monkeypatch.setenv("GITEA_TOKEN", "local-test-token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    monkeypatch.setattr(remote, "_caller_fingerprint", lambda: fingerprint)
    audit = Audit(client.order, failed=failed)
    monkeypatch.setattr(remote, "_get_gitea_audit_logger", lambda: audit)
    return audit


async def close_issue(*, updated=UPDATED, reason=REASON):
    return await remote.gitea_close_issue(
        "owner", "repo", 7, expected_updated_at=updated, reason=reason
    )


@pytest.mark.asyncio
async def test_success_has_fenced_intent_then_single_close_and_verified_result(monkeypatch):
    client = Client()
    audit = configure(monkeypatch, client)
    result = await close_issue()
    assert result["ok"] is True
    assert result["result"]["verified"] is True
    assert result["result"]["closed"] is True
    assert result["result"]["already_closed"] is False
    assert client.patches == 1
    assert client.order == ["read", "identity", "read", "audit:intent", "patch", "read", "audit:success"]
    assert len(audit.intents) == 1
    metadata = audit.intents[0].metadata
    assert metadata["gitea_username"] == "release-owner"
    assert metadata["caller_fingerprint"] == FP
    assert metadata["expected_updated_at"] == UPDATED
    assert len(audit.successes) == 1


@pytest.mark.asyncio
async def test_already_closed_idempotent_without_audit_or_patch(monkeypatch):
    client = Client(state="closed", updated="2026-11-05T10:02:00+03:00")
    audit = configure(monkeypatch, client)
    result = await close_issue()
    assert result["ok"] is True
    assert result["result"]["already_closed"] is True
    assert client.patches == 0
    assert not audit.intents


@pytest.mark.asyncio
@pytest.mark.parametrize("state,updated,pr", [
    ("open", "2026-11-05T10:04:00+03:00", False),
    ("unknown", UPDATED, False),
    ("open", UPDATED, True),
])
async def test_stale_wrong_state_or_pull_request_never_mutates(monkeypatch, state, updated, pr):
    client = Client(state=state, updated=updated, pr=pr)
    audit = configure(monkeypatch, client)
    result = await close_issue()
    assert result["ok"] is False
    assert client.patches == 0
    assert not audit.intents


@pytest.mark.asyncio
async def test_missing_attribution_fails_before_intent(monkeypatch):
    client = Client()
    audit = configure(monkeypatch, client, fingerprint=None)
    result = await close_issue()
    assert result["ok"] is False
    assert result["error"]["code"] == "AUTH_ERROR"
    assert client.patches == 0
    assert not audit.intents


@pytest.mark.asyncio
async def test_audit_failure_fails_closed_and_hides_secrets(monkeypatch):
    client = Client()
    audit = configure(monkeypatch, client, failed=True)
    result = await close_issue()
    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert client.patches == 0
    assert len(audit.intents) == 1
    assert "/path" not in str(result)
    assert "token" not in str(result)


@pytest.mark.asyncio
async def test_changed_issue_between_preflight_reads_fails_with_no_audit(monkeypatch):
    client = Client(drift_at_second=True)
    audit = configure(monkeypatch, client)
    result = await close_issue()
    assert result["ok"] is False
    assert result["error"]["code"] == "STALE_ISSUE"
    assert client.reads == 2
    assert client.patches == 0
    assert not audit.intents


@pytest.mark.asyncio
async def test_patch_response_lost_is_outcome_unknown_not_retryable(monkeypatch):
    client = Client(raise_after_close=True)
    audit = configure(monkeypatch, client)
    result = await close_issue()
    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert client.patches == 1
    assert client.state == "closed"
    assert len(audit.intents) == 1


@pytest.mark.asyncio
async def test_missing_confirmation_never_reports_closed_success(monkeypatch):
    client = Client(apply=False)
    configure(monkeypatch, client)
    result = await close_issue()
    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert client.patches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("updated,reason", [
    ("not-a-timestamp", REASON),
    (UPDATED, "x"),
    (UPDATED, "approved\nfor closure"),
])
async def test_bad_input_rejected_without_remote_calls(monkeypatch, updated, reason):
    client = Client()
    audit = configure(monkeypatch, client)
    result = await close_issue(updated=updated, reason=reason)
    assert result["ok"] is False
    assert client.reads == 0
    assert client.patches == 0
    assert not audit.intents

