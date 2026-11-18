from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_audit import AuditWriteError, McpAuditEvent
from examples.mcp_server.mcp_infra.adapters import remote
from examples.mcp_server.tool_modes import MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS, TOOL_NAMES_BY_MODE
from examples.mcp_server.tool_scopes import get_required_scopes

SHA = "a" * 40
RUN_ID = 7001
ATTEMPT = 2
FINGERPRINT = "ab" * 32
CANCEL_ENDPOINT = "/repos/{owner}/{repo}/actions/runs/{run_id}/cancel"


def _run(*, head_sha=SHA, run_attempt=ATTEMPT, status="in_progress", conclusion=None):
    return {
        "id": RUN_ID,
        "run_attempt": run_attempt,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "event": "pull_request",
        "html_url": f"https://git.example/actions/runs/{RUN_ID}",
    }


class Audit:
    def __init__(self, *, fail_intent: bool = False):
        self.fail_intent = fail_intent
        self.required: list[McpAuditEvent] = []
        self.appended: list[McpAuditEvent] = []

    def append_required(self, event: McpAuditEvent) -> None:
        self.required.append(event)
        if self.fail_intent:
            raise AuditWriteError("audit unavailable")

    def append(self, event: McpAuditEvent) -> None:
        self.appended.append(event)


class FakeClient:
    def __init__(
        self,
        token="token",
        *,
        before=None,
        after=None,
        mutation_error: BaseException | None = None,
        apply_mutation: bool = True,
        jobs_after: dict | None = None,
    ):
        assert token == "token"
        self.before = before or _run()
        self.after = after or _run(status="completed", conclusion="cancelled")
        self.mutation_error = mutation_error
        self.apply_mutation = apply_mutation
        self.jobs_after = jobs_after
        self.mutated = False
        self.cancel_calls: list[tuple[str, str, int]] = []
        self.reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_user(self):
        return {"login": "robot"}

    async def get_action_run(self, owner, repo, run_id):
        self.reads += 1
        return dict(self.after if self.mutated else self.before)

    async def list_action_run_jobs(self, owner, repo, run_id):
        if self.jobs_after is not None:
            return dict(self.jobs_after)
        terminal = (
            (self.mutated and self.after.get("status") == "completed")
            or (not self.mutated and self.before.get("status") == "completed")
        )
        return {
            "total_count": 3,
            "jobs": [
                {
                    "id": number, "run_id": RUN_ID, "head_sha": SHA,
                    "run_attempt": ATTEMPT, "name": name,
                    "status": "completed" if terminal else status,
                    "conclusion": "cancelled" if terminal else (None if status != "completed" else "success"),
                }
                for number, name, status in (
                    (1, "unit", "requested"),
                    (2, "fleet", "in_progress"),
                    (3, "build", "completed"),
                )
            ],
        }

    async def cancel_action_run(self, owner, repo, run_id):
        self.cancel_calls.append((owner, repo, run_id))
        if self.apply_mutation:
            self.mutated = True
        if self.mutation_error is not None:
            raise self.mutation_error
        return dict(self.after)


def _setup(monkeypatch, client: FakeClient, audit: Audit | None = None):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    monkeypatch.setattr(remote, "_caller_fingerprint", lambda: FINGERPRINT)
    logger = audit or Audit()
    monkeypatch.setattr(remote, "_get_gitea_audit_logger", lambda: logger)
    return logger


@pytest.mark.asyncio
async def test_client_cancel_uses_dedicated_endpoint_and_force_cancel_is_denied(monkeypatch):
    client = GiteaClient("token")
    post = AsyncMock(return_value=_run(status="completed", conclusion="cancelled"))
    monkeypatch.setattr(client, "_post_action", post)
    try:
        result = await client.cancel_action_run("owner", "repo", RUN_ID)
        assert result["id"] == RUN_ID
        post.assert_awaited_once_with(CANCEL_ENDPOINT, owner="owner", repo="repo", run_id=RUN_ID)
    finally:
        await client.aclose()

    policy_client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="Actions write endpoint not allowed"):
            await policy_client._post_action(
                "/repos/{owner}/{repo}/actions/runs/{run_id}/force-cancel",
                owner="owner",
                repo="repo",
                run_id=RUN_ID,
            )
    finally:
        await policy_client.aclose()


@pytest.mark.asyncio
async def test_cancel_happy_path_is_audited_and_proves_settled_jobs(monkeypatch):
    client = FakeClient()
    audit = _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)

    assert result["ok"] is True
    body = result["result"]
    assert body["cancelled"] is True
    assert body["outcome"] == "cancelled"
    assert body["verified"] is True
    assert body["jobs_summary"]["active"] == []
    assert body["jobs_terminal_verified"] is True
    assert client.cancel_calls == [("owner", "repo", RUN_ID)]
    assert [event.event_type for event in audit.required] == ["mcp.gitea_destructive_intent"]
    assert [event.event_type for event in audit.appended] == ["mcp.gitea_destructive_success"]
    assert "token" not in repr(audit.required[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expected_sha",
    ["A" * 40, " a" * 20, "a" * 39, "g" * 40],
)
async def test_cancel_requires_exact_lowercase_sha(monkeypatch, expected_sha):
    client = FakeClient()
    audit = _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run(
        "owner", "repo", RUN_ID, expected_sha, ATTEMPT
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert client.reads == 0
    assert client.cancel_calls == []
    assert audit.required == []


@pytest.mark.asyncio
async def test_cancel_head_and_attempt_leases_fail_before_mutation(monkeypatch):
    for before, code in (
        (_run(head_sha="b" * 40), "HEAD_MISMATCH"),
        (_run(run_attempt=ATTEMPT + 1), "RUN_STATE_UNPROVEN"),
    ):
        client = FakeClient(before=before)
        audit = _setup(monkeypatch, client)
        result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)
        assert result["ok"] is False
        assert result["error"]["code"] == code
        assert client.cancel_calls == []
        assert audit.required == []


@pytest.mark.asyncio
async def test_cancel_terminal_run_is_idempotent_without_audit_or_mutation(monkeypatch):
    client = FakeClient(before=_run(status="completed", conclusion="success"))
    audit = _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)

    assert result["ok"] is True
    assert result["result"]["already_terminal"] is True
    assert result["result"]["jobs_terminal_verified"] is True
    assert result["result"]["cancelled"] is False
    assert client.cancel_calls == []
    assert audit.required == []


@pytest.mark.asyncio
async def test_cancel_audit_failure_is_zero_mutation(monkeypatch):
    client = FakeClient()
    audit = _setup(monkeypatch, client, Audit(fail_intent=True))
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)

    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert client.cancel_calls == []
    assert len(audit.required) == 1


@pytest.mark.asyncio
async def test_cancel_ambiguous_response_reconciles_without_replay(monkeypatch):
    request = httpx.Request("POST", f"https://git.example/actions/runs/{RUN_ID}/cancel")
    client = FakeClient(
        mutation_error=httpx.ReadTimeout("lost response", request=request),
        apply_mutation=True,
    )
    _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)

    assert result["ok"] is True
    assert result["result"]["outcome"] == "cancelled_after_ambiguous_response"
    assert result["result"]["jobs_terminal_verified"] is True
    assert client.cancel_calls == [("owner", "repo", RUN_ID)]


@pytest.mark.asyncio
async def test_cancel_ambiguous_unproven_result_forbids_blind_retry(monkeypatch):
    request = httpx.Request("POST", f"https://git.example/actions/runs/{RUN_ID}/cancel")
    client = FakeClient(
        mutation_error=httpx.ReadTimeout("lost response", request=request),
        apply_mutation=False,
    )
    _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)

    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert "Do not replay" in result["error"]["hint"]
    assert client.cancel_calls == [("owner", "repo", RUN_ID)]
    assert client.reads == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_status", ["queued", "in_progress"])
async def test_cancel_acknowledged_but_runner_cleanup_pending_is_distinct(monkeypatch, pending_status):
    client = FakeClient(after=_run(status=pending_status))
    audit = _setup(monkeypatch, client)

    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)

    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "ACTION_CANCEL_ACCEPTED_PENDING"
    assert error["retryable"] is False
    details = error["details"]
    assert details["dispatch_acknowledged"] is True
    assert details["terminal_verified"] is False
    assert details["cancelled"] is False
    assert details["observed"]["status"] == pending_status
    assert details["observed"]["head_sha"] == SHA
    assert details["observed"]["run_attempt"] == ATTEMPT
    assert client.cancel_calls == [("owner", "repo", RUN_ID)]
    assert client.reads == 4
    assert [event.event_type for event in audit.appended] == []


@pytest.mark.asyncio
async def test_cancel_ambiguous_timeout_stays_unknown_even_if_run_is_queued(monkeypatch):
    request = httpx.Request("POST", f"https://git.example/actions/runs/{RUN_ID}/cancel")
    client = FakeClient(
        after=_run(status="queued"),
        mutation_error=httpx.ReadTimeout("lost response", request=request),
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)

    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["details"]["observed"]["status"] == "queued"
    assert client.cancel_calls == [("owner", "repo", RUN_ID)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after",
    [
        _run(head_sha="b" * 40, status="queued"),
        _run(run_attempt=ATTEMPT + 1, status="queued"),
    ],
)
async def test_cancel_acknowledgement_never_trusts_identity_drift(monkeypatch, after):
    client = FakeClient(after=after)
    _setup(monkeypatch, client)

    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)

    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["details"].get("dispatch_acknowledged") is not True
    assert client.cancel_calls == [("owner", "repo", RUN_ID)]


@pytest.mark.asyncio
async def test_cancel_natural_completion_race_is_not_mislabeled_cancelled(monkeypatch):
    client = FakeClient(after=_run(status="completed", conclusion="success"))
    _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)

    assert result["ok"] is True
    assert result["result"]["cancelled"] is False
    assert result["result"]["outcome"] == "terminal_after_cancel_attempt"
    assert result["result"]["conclusion"] == "success"


def test_cancel_tool_is_write_admin_only():
    assert "gitea_cancel_action_run" in MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS
    assert "gitea_cancel_action_run" in TOOL_NAMES_BY_MODE["mcp_client_write"]
    assert "gitea_cancel_action_run" not in TOOL_NAMES_BY_MODE["mcp_client"]
    assert get_required_scopes("gitea_cancel_action_run") == ["mcp:repo", "mcp:admin"]


def _jobs_with_one_active() -> dict:
    return {"total_count": 2, "jobs": [
        {"id": 21, "run_id": RUN_ID, "head_sha": SHA, "run_attempt": ATTEMPT,
         "name": "unit", "status": "completed", "conclusion": "failure"},
        {"id": 22, "run_id": RUN_ID, "head_sha": SHA, "run_attempt": ATTEMPT,
         "name": "cleanup", "status": "in_progress", "conclusion": None},
    ]}


@pytest.mark.asyncio
async def test_already_terminal_run_with_active_jobs_is_not_cleanly_verified(monkeypatch):
    client = FakeClient(before=_run(status="completed", conclusion="cancelled"),
                        jobs_after=_jobs_with_one_active())
    audit = _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)
    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_STATE_UNPROVEN"
    assert result["error"]["details"]["reason"] == "jobs_nonterminal"
    assert result["error"]["details"]["mutation_started"] is False
    assert client.cancel_calls == []
    assert audit.required == []


@pytest.mark.asyncio
async def test_cancel_confirmed_terminal_run_but_active_jobs_remains_pending(monkeypatch):
    client = FakeClient(jobs_after=_jobs_with_one_active())
    audit = _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)
    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_CANCEL_ACCEPTED_PENDING"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["dispatch_acknowledged"] is True
    assert result["error"]["details"]["terminal_verified"] is False
    assert result["error"]["details"]["jobs_reason"] == "jobs_nonterminal"
    assert client.cancel_calls == [("owner", "repo", RUN_ID)]
    assert [event.event_type for event in audit.appended] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,reason", [
    ({"total_count": 3, "jobs": _jobs_with_one_active()["jobs"]}, "jobs_unproven"),
    ({"total_count": 1, "jobs": [_jobs_with_one_active()["jobs"][0] | {"head_sha": "b" * 40}]}, "jobs_unproven"),
    ({"total_count": 0, "jobs": []}, "jobs_unproven"),
])
async def test_post_cancel_incomplete_or_foreign_job_set_never_claims_cleanup(monkeypatch, payload, reason):
    client = FakeClient(jobs_after=payload)
    _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)
    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_CANCEL_ACCEPTED_PENDING"
    assert result["error"]["details"]["jobs_reason"] == reason
    assert result["error"]["details"]["terminal_verified"] is False
    assert len(client.cancel_calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_cancel_with_active_jobs_remains_unknown_not_retryable(monkeypatch):
    request = httpx.Request("POST", f"https://git.example/actions/runs/{RUN_ID}/cancel")
    client = FakeClient(
        jobs_after=_jobs_with_one_active(),
        mutation_error=httpx.ReadTimeout("hidden", request=request),
        apply_mutation=True,
    )
    audit = _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)
    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert len(client.cancel_calls) == 1
    assert audit.appended == []


@pytest.mark.asyncio
async def test_already_terminal_partial_jobs_is_not_idempotent_cleanup_proof(monkeypatch):
    client = FakeClient(before=_run(status="completed", conclusion="success"),
                        jobs_after={"total_count": 2, "jobs": _jobs_with_one_active()["jobs"][:1]})
    _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)
    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_STATE_UNPROVEN"
    assert result["error"]["details"]["reason"] == "jobs_unproven"
    assert client.cancel_calls == []


@pytest.mark.asyncio
async def test_completed_run_with_live_old_attempt_does_not_claim_cancel_cleanup(monkeypatch):
    old_running = _jobs_with_one_active()["jobs"][1] | {"run_attempt": ATTEMPT - 1}
    current_done = _jobs_with_one_active()["jobs"][0]
    client = FakeClient(jobs_after={"total_count": 2, "jobs": [old_running, current_done]})
    audit = _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)
    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_CANCEL_ACCEPTED_PENDING"
    assert result["error"]["details"]["jobs_reason"] == "jobs_nonterminal"
    assert result["error"]["details"]["terminal_verified"] is False
    assert client.cancel_calls == [("owner", "repo", RUN_ID)]
    assert audit.appended == []


@pytest.mark.asyncio
async def test_cancel_jobs_read_failure_after_post_is_pending_without_token_leak(monkeypatch):
    class FailingClient(FakeClient):
        async def list_action_run_jobs(self, owner, repo, run_id):
            raise TimeoutError("token-secret-must-not-appear")

    client = FailingClient()
    _setup(monkeypatch, client)
    result = await remote.gitea_cancel_action_run("owner", "repo", RUN_ID, SHA, ATTEMPT)
    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_CANCEL_ACCEPTED_PENDING"
    assert result["error"]["details"]["jobs_reason"] == "jobs_unavailable"
    assert result["error"]["retryable"] is False
    assert "token-secret" not in repr(result)
    assert len(client.cancel_calls) == 1
