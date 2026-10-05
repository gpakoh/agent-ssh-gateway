"""Guarded Gitea Actions rerun write contract."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_infra.adapters import remote
from examples.mcp_server.tool_modes import MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS, TOOL_NAMES_BY_MODE
from examples.mcp_server.tool_scopes import get_required_scopes

SHA = "a" * 40
RUN_ID = 13880


def _run_payload(
    *,
    run_id: object = RUN_ID,
    head_sha: object = SHA,
    run_attempt: object = 1,
    status: object = "completed",
    conclusion: object = "failure",
) -> dict[str, object]:
    return {
        "id": run_id,
        "run_number": 1618,
        "run_attempt": run_attempt,
        "name": "CI",
        "event": "pull_request",
        "status": status,
        "conclusion": conclusion,
        "head_branch": None,
        "head_sha": head_sha,
        "actor": {"login": "robot"},
        "trigger_actor": {"login": "robot"},
        "repository": {"name": "repo", "full_name": "owner/repo"},
        "started_at": "2026-10-05T03:48:04+03:00",
        "completed_at": "2026-10-05T04:25:37+03:00" if status == "completed" else None,
        "html_url": f"https://git.example/actions/runs/{RUN_ID}",
    }


class FakeRerunClient:
    def __init__(
        self,
        token: str,
        *,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
        mutation_error: BaseException | None = None,
        apply_mutation: bool = True,
    ) -> None:
        assert token == "token"
        self.before = before or _run_payload()
        self.after = after or _run_payload(
            run_attempt=2,
            status="in_progress",
            conclusion=None,
        )
        self.mutation_error = mutation_error
        self.apply_mutation = apply_mutation
        self.mutated = False
        self.rerun_calls: list[tuple[str, str, int]] = []
        self.reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_action_run(self, owner: str, repo: str, run_id: int):
        self.reads += 1
        return dict(self.after if self.mutated else self.before)

    async def rerun_action_run(self, owner: str, repo: str, run_id: int):
        self.rerun_calls.append((owner, repo, run_id))
        if self.apply_mutation:
            self.mutated = True
        if self.mutation_error is not None:
            raise self.mutation_error
        return dict(self.after)


def _setup(monkeypatch, client: FakeRerunClient) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)


@pytest.mark.asyncio
async def test_client_rerun_uses_only_dedicated_endpoint(monkeypatch):
    client = GiteaClient("token")
    post_action = AsyncMock(return_value=_run_payload(run_attempt=2, status="in_progress", conclusion=None))
    monkeypatch.setattr(client, "_post_action", post_action)
    try:
        result = await client.rerun_action_run("owner", "repo", RUN_ID)
    finally:
        await client.aclose()

    assert result["id"] == RUN_ID
    assert result["run_attempt"] == 2
    post_action.assert_awaited_once_with(
        "/repos/{owner}/{repo}/actions/runs/{run_id}/rerun",
        owner="owner",
        repo="repo",
        run_id=RUN_ID,
    )


@pytest.mark.asyncio
async def test_client_rerun_rejects_bad_run_id_before_post(monkeypatch):
    client = GiteaClient("token")
    post_action = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_post_action", post_action)
    try:
        for bad in (0, -1, True, "1"):
            with pytest.raises(ValueError, match="run_id must be a positive integer"):
                await client.rerun_action_run("owner", "repo", bad)  # type: ignore[arg-type]
    finally:
        await client.aclose()
    post_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_action_post_rejects_nonallowlisted_endpoint():
    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="Actions write endpoint not allowed"):
            await client._post_action(
                "/repos/{owner}/{repo}/pulls",
                owner="owner",
                repo="repo",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_adapter_reruns_exact_failed_head_once_and_proves_attempt(monkeypatch):
    client = FakeRerunClient("token")
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)

    assert result["ok"] is True
    assert client.rerun_calls == [("owner", "repo", RUN_ID)]
    assert result["result"]["previous_attempt"] == 1
    assert result["result"]["run_attempt"] == 2
    assert result["result"]["head_sha"] == SHA
    assert result["result"]["verified"] is True
    assert result["result"]["outcome"] == "started"


@pytest.mark.asyncio
async def test_adapter_head_mismatch_is_zero_mutation(monkeypatch):
    client = FakeRerunClient("token", before=_run_payload(head_sha="b" * 40))
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert client.rerun_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_active_run_without_mutation(monkeypatch):
    client = FakeRerunClient(
        "token",
        before=_run_payload(status="in_progress", conclusion=None),
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_NOT_COMPLETED"
    assert client.rerun_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_successful_run_without_mutation(monkeypatch):
    client = FakeRerunClient("token", before=_run_payload(conclusion="success"))
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_ALREADY_SUCCESSFUL"
    assert client.rerun_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("conclusion", [None, "skipped", "neutral", 1])
async def test_adapter_rejects_unproven_rerunnable_conclusion(monkeypatch, conclusion):
    client = FakeRerunClient("token", before=_run_payload(conclusion=conclusion))
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "RUN_NOT_RERUNNABLE"
    assert client.rerun_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value,code",
    [
        ("id", True, "RUN_ID_MISMATCH"),
        ("id", "13880", "RUN_ID_MISMATCH"),
        ("run_attempt", True, "RUN_STATE_UNPROVEN"),
        ("run_attempt", -1, "RUN_STATE_UNPROVEN"),
        ("run_attempt", "1", "RUN_STATE_UNPROVEN"),
    ],
)
async def test_adapter_malformed_remote_identity_or_attempt_fails_closed(
    monkeypatch, field, value, code
):
    before = _run_payload()
    before[field] = value
    client = FakeRerunClient("token", before=before)
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert client.rerun_calls == []


@pytest.mark.asyncio
async def test_adapter_transport_loss_after_applied_rerun_reconciles_without_replay(monkeypatch):
    request = httpx.Request("POST", "https://git.example/actions/runs/13880/rerun")
    client = FakeRerunClient(
        "token",
        mutation_error=httpx.ReadTimeout("lost response", request=request),
        apply_mutation=True,
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)

    assert result["ok"] is True
    assert client.rerun_calls == [("owner", "repo", RUN_ID)]
    assert result["result"]["outcome"] == "started_after_ambiguous_response"
    assert result["result"]["run_attempt"] == 2


@pytest.mark.asyncio
async def test_adapter_unproven_ambiguous_rerun_forbids_blind_retry(monkeypatch):
    request = httpx.Request("POST", "https://git.example/actions/runs/13880/rerun")
    client = FakeRerunClient(
        "token",
        mutation_error=httpx.ReadTimeout("lost response", request=request),
        apply_mutation=False,
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert client.rerun_calls == [("owner", "repo", RUN_ID)]
    assert client.reads == 4  # preflight + three bounded reconciliation reads


@pytest.mark.asyncio
async def test_adapter_requires_token_and_valid_inputs(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    missing = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, SHA)
    assert missing["error"]["code"] == "DEPENDENCY_MISSING"

    monkeypatch.setenv("GITEA_TOKEN", "token")
    for bad_id in (0, True):
        bad = await remote.gitea_rerun_action_run("owner", "repo", bad_id, SHA)  # type: ignore[arg-type]
        assert bad["error"]["code"] == "INVALID_INPUT"
    for bad_sha in ("abc", True, None):
        bad = await remote.gitea_rerun_action_run("owner", "repo", RUN_ID, bad_sha)  # type: ignore[arg-type]
        assert bad["error"]["code"] == "INVALID_INPUT"


def test_rerun_tool_is_write_admin_only():
    assert "gitea_rerun_action_run" in MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS
    assert "gitea_rerun_action_run" not in TOOL_NAMES_BY_MODE["mcp_client"]
    assert "gitea_rerun_action_run" in TOOL_NAMES_BY_MODE["mcp_client_write"]
    assert get_required_scopes("gitea_rerun_action_run") == ["mcp:repo", "mcp:admin"]
