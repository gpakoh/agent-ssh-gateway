"""Guarded Gitea Actions secret provisioning contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_audit import AuditWriteError
from examples.mcp_server.mcp_infra.adapters import remote
from examples.mcp_server.secret_sources import ApprovedSecretSource
from examples.mcp_server.tool_modes import MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS, TOOL_NAMES_BY_MODE
from examples.mcp_server.tool_scopes import get_required_scopes

SOURCE_ID = "registry_token"
OWNER = "gpakoh"
REPO = "work-session-service"
SECRET_NAME = "REGISTRY_TOKEN"
SECRET_VALUE = "registry-token-super-secret-value"
SOURCE_SHA = "a" * 64
DESCRIPTION = f"mcp-source-sha256:{SOURCE_SHA}"


class FakeAuditLogger:
    def __init__(self, *, fail_required: bool = False) -> None:
        self.fail_required = fail_required
        self.required = []
        self.best_effort = []

    def append_required(self, event) -> None:
        if self.fail_required:
            raise AuditWriteError("audit unavailable")
        self.required.append(event)

    def append(self, event) -> None:
        self.best_effort.append(event)


class FakeSecretClient:
    def __init__(
        self,
        *,
        mutation_error: BaseException | None = None,
        apply_mutation: bool = True,
        admin: bool = True,
    ) -> None:
        self.mutation_error = mutation_error
        self.apply_mutation = apply_mutation
        self.admin = admin
        self.secrets: dict[str, dict[str, object]] = {}
        self.put_calls: list[dict[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_user(self):
        return {"login": "robot"}

    async def get_repo(self, owner: str, repo: str):
        return {"archived": False, "permissions": {"admin": self.admin, "push": True}}

    async def list_action_secrets(self, owner: str, repo: str, *, page: int = 1, limit: int = 50):
        if page > 1:
            return []
        return list(self.secrets.values())

    async def put_action_secret(
        self,
        owner: str,
        repo: str,
        *,
        secret_name: str,
        value: str,
        description: str,
    ) -> None:
        self.put_calls.append(
            {
                "owner": owner,
                "repo": repo,
                "secret_name": secret_name,
                "value": value,
                "description": description,
            }
        )
        if self.apply_mutation:
            self.secrets[secret_name] = {
                "name": secret_name,
                "description": description,
                "created_at": "2026-10-11T12:00:00Z",
            }
        if self.mutation_error is not None:
            raise self.mutation_error


def _source(sha: str = SOURCE_SHA) -> ApprovedSecretSource:
    return ApprovedSecretSource(source_id=SOURCE_ID, value=SECRET_VALUE, sha256=sha)


def _setup(
    monkeypatch,
    client: FakeSecretClient,
    audit: FakeAuditLogger | None = None,
    *,
    source_sha: str = SOURCE_SHA,
) -> FakeAuditLogger:
    logger = audit or FakeAuditLogger()
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    monkeypatch.setattr(remote, "_caller_fingerprint", lambda: "caller-fingerprint")
    monkeypatch.setattr(remote, "_get_gitea_audit_logger", lambda: logger)
    monkeypatch.setattr(remote, "read_approved_secret_source", lambda source_id: _source(source_sha))
    return logger


@pytest.mark.asyncio
async def test_client_put_action_secret_uses_dedicated_put(monkeypatch):
    client = GiteaClient("token")
    put = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_put_action_secret", put)
    try:
        await client.put_action_secret(
            OWNER,
            REPO,
            secret_name=SECRET_NAME,
            value=SECRET_VALUE,
            description=DESCRIPTION,
        )
    finally:
        await client.aclose()

    put.assert_awaited_once_with(
        "/repos/{owner}/{repo}/actions/secrets/{secretname}",
        {"data": SECRET_VALUE, "description": DESCRIPTION},
        owner=OWNER,
        repo=REPO,
        secretname=SECRET_NAME,
    )


@pytest.mark.asyncio
async def test_client_rejects_reserved_secret_name_before_put(monkeypatch):
    client = GiteaClient("token")
    put = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_put_action_secret", put)
    try:
        with pytest.raises(ValueError, match="invalid Gitea Actions secret name"):
            await client.put_action_secret(
                OWNER,
                REPO,
                secret_name="GITEA_TOKEN",
                value=SECRET_VALUE,
                description=DESCRIPTION,
            )
    finally:
        await client.aclose()
    put.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_status_returns_only_fingerprint(monkeypatch):
    monkeypatch.setattr(remote, "read_approved_secret_source", lambda source_id: _source())

    result = await remote.gitea_actions_secret_source_status(SOURCE_ID)

    assert result["ok"] is True
    assert result["result"] == {
        "source_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
        "available": True,
    }
    assert result["meta"]["redacted"] is True
    assert SECRET_VALUE not in repr(result)


@pytest.mark.asyncio
async def test_provision_secret_happy_path_is_redacted_and_audited(monkeypatch):
    client = FakeSecretClient()
    audit = _setup(monkeypatch, client)

    result = await remote.gitea_provision_actions_secret(
        OWNER, REPO, SECRET_NAME, SOURCE_ID, SOURCE_SHA, "sync registry credential"
    )

    assert result["ok"] is True
    data = result["result"]
    assert data["verified"] is True
    assert data["target_fingerprint_attested"] is True
    assert data["source_sha256"] == SOURCE_SHA
    assert SECRET_VALUE not in repr(result)
    assert client.put_calls[0]["value"] == SECRET_VALUE
    assert client.put_calls[0]["description"] == DESCRIPTION
    assert len(audit.required) == 1
    assert len(audit.best_effort) == 1
    assert SECRET_VALUE not in repr(audit.required[0])
    assert audit.required[0].metadata["source_sha256"] == SOURCE_SHA


@pytest.mark.asyncio
async def test_provision_secret_denies_unapproved_target_without_mutation(monkeypatch):
    client = FakeSecretClient()
    _setup(monkeypatch, client)

    result = await remote.gitea_provision_actions_secret(
        OWNER, "other-repo", SECRET_NAME, SOURCE_ID, SOURCE_SHA, "not approved"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert result["error"]["details"]["mutation_occurred"] is False
    assert client.put_calls == []


@pytest.mark.asyncio
async def test_provision_secret_rejects_source_hash_drift_without_mutation(monkeypatch):
    client = FakeSecretClient()
    _setup(monkeypatch, client, source_sha="b" * 64)

    result = await remote.gitea_provision_actions_secret(
        OWNER, REPO, SECRET_NAME, SOURCE_ID, SOURCE_SHA, "stale lease"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "SOURCE_HASH_MISMATCH"
    assert client.put_calls == []


@pytest.mark.asyncio
async def test_provision_secret_audit_failure_is_zero_mutation(monkeypatch):
    client = FakeSecretClient()
    audit = FakeAuditLogger(fail_required=True)
    _setup(monkeypatch, client, audit)

    result = await remote.gitea_provision_actions_secret(
        OWNER, REPO, SECRET_NAME, SOURCE_ID, SOURCE_SHA, "audit required"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert client.put_calls == []


@pytest.mark.asyncio
async def test_provision_secret_transport_loss_reconciles_applied_mutation(monkeypatch):
    request = httpx.Request("PUT", "https://git.example/api/v1/repos/gpakoh/work-session-service/actions/secrets/REGISTRY_TOKEN")
    client = FakeSecretClient(
        mutation_error=httpx.ReadTimeout("lost response", request=request),
        apply_mutation=True,
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_provision_actions_secret(
        OWNER, REPO, SECRET_NAME, SOURCE_ID, SOURCE_SHA, "reconcile response loss"
    )

    assert result["ok"] is True
    assert result["result"]["outcome"] == "completed_after_ambiguous_response"
    assert len(client.put_calls) == 1


@pytest.mark.asyncio
async def test_provision_secret_unknown_outcome_never_blindly_replays(monkeypatch):
    request = httpx.Request("PUT", "https://git.example/api/v1/repos/gpakoh/work-session-service/actions/secrets/REGISTRY_TOKEN")
    client = FakeSecretClient(
        mutation_error=httpx.ReadTimeout("lost response", request=request),
        apply_mutation=False,
    )
    _setup(monkeypatch, client)
    monkeypatch.setattr(remote, "_GITEA_SECRET_RECONCILE_DELAY_SECONDS", 0)

    result = await remote.gitea_provision_actions_secret(
        OWNER, REPO, SECRET_NAME, SOURCE_ID, SOURCE_SHA, "unknown result"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert len(client.put_calls) == 1
    assert SECRET_VALUE not in repr(result)


def test_actions_secret_tools_are_write_admin_surface_only():
    for tool in (
        "gitea_actions_secret_source_status",
        "gitea_provision_actions_secret",
    ):
        assert tool in MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS
        assert tool not in TOOL_NAMES_BY_MODE["mcp_client"]
        assert tool in TOOL_NAMES_BY_MODE["mcp_client_write"]
        assert get_required_scopes(tool) == ["mcp:repo", "mcp:admin"]
