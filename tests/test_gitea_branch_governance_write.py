"""Guarded Gitea branch-governance write contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_audit import AuditWriteError
from examples.mcp_server.mcp_infra.adapters import remote
from examples.mcp_server.tool_modes import MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS, TOOL_NAMES_BY_MODE
from examples.mcp_server.tool_scopes import get_required_scopes

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
OTHER_SHA = "c" * 40
OLD_BRANCH = "fix/deep-review-upgrade-backfill-20260911"
NEW_BRANCH = "main"


def _branch(name: str, sha: str) -> dict[str, object]:
    return {
        "name": name,
        "commit": {"id": sha},
        "protected": False,
        "effective_branch_protection_name": "",
    }


def _not_found(path: str = "/branches/main") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://git.example{path}")
    response = httpx.Response(404, request=request)
    return httpx.HTTPStatusError("not found", request=request, response=response)


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


class FakeGovernanceClient:
    def __init__(
        self,
        *,
        default_branch: str = OLD_BRANCH,
        branches: dict[str, str] | None = None,
        commits: set[str] | None = None,
        permissions: dict[str, bool] | None = None,
        create_error: BaseException | None = None,
        default_error: BaseException | None = None,
        apply_create: bool = True,
        apply_default: bool = True,
    ) -> None:
        self.default_branch = default_branch
        self.branches = branches or {OLD_BRANCH: OLD_SHA}
        self.commits = commits or {OLD_SHA, NEW_SHA}
        self.permissions = permissions or {"push": True, "admin": True}
        self.create_error = create_error
        self.default_error = default_error
        self.apply_create = apply_create
        self.apply_default = apply_default
        self.create_calls: list[tuple[str, str, str, str]] = []
        self.default_calls: list[tuple[str, str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_user(self):
        return {"login": "robot"}

    async def get_repo(self, owner: str, repo: str):
        return {
            "default_branch": self.default_branch,
            "archived": False,
            "permissions": dict(self.permissions),
        }

    async def get_branch(self, owner: str, repo: str, branch: str):
        sha = self.branches.get(branch)
        if sha is None:
            raise _not_found(f"/branches/{branch}")
        return _branch(branch, sha)

    async def list_commits(self, owner: str, repo: str, sha: str | None = None, limit: int = 30):
        if sha in self.commits:
            return [{"sha": sha}]
        return []

    async def create_branch_at_ref(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
        source_sha: str,
    ):
        self.create_calls.append((owner, repo, branch, source_sha))
        if self.apply_create:
            self.branches[branch] = source_sha
        if self.create_error is not None:
            raise self.create_error
        return _branch(branch, source_sha)

    async def set_default_branch(self, owner: str, repo: str, *, branch: str):
        self.default_calls.append((owner, repo, branch))
        if self.apply_default:
            self.default_branch = branch
        if self.default_error is not None:
            raise self.default_error
        return {"default_branch": branch}


def _setup(monkeypatch, client: FakeGovernanceClient, audit: FakeAuditLogger | None = None) -> FakeAuditLogger:
    logger = audit or FakeAuditLogger()
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    monkeypatch.setattr(remote, "_caller_fingerprint", lambda: "caller-fingerprint")
    monkeypatch.setattr(remote, "_get_gitea_audit_logger", lambda: logger)
    return logger


@pytest.mark.asyncio
async def test_client_create_branch_uses_dedicated_allowlist(monkeypatch):
    client = GiteaClient("token")
    post = AsyncMock(return_value=_branch(NEW_BRANCH, NEW_SHA))
    monkeypatch.setattr(client, "_post_branch", post)
    try:
        result = await client.create_branch_at_ref(
            "owner", "repo", branch=NEW_BRANCH, source_sha=NEW_SHA
        )
    finally:
        await client.aclose()

    assert result["name"] == NEW_BRANCH
    post.assert_awaited_once_with(
        "/repos/{owner}/{repo}/branches",
        {"new_branch_name": NEW_BRANCH, "old_ref_name": NEW_SHA},
        owner="owner",
        repo="repo",
    )


@pytest.mark.asyncio
async def test_client_create_branch_rejects_non_exact_sha_before_post(monkeypatch):
    client = GiteaClient("token")
    post = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_post_branch", post)
    try:
        with pytest.raises(ValueError, match="lowercase 40-character SHA-1"):
            await client.create_branch_at_ref(
                "owner", "repo", branch=NEW_BRANCH, source_sha=NEW_SHA.upper()
            )
    finally:
        await client.aclose()
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_default_switch_uses_dedicated_admin_patch(monkeypatch):
    client = GiteaClient("token")
    patch = AsyncMock(return_value={"default_branch": NEW_BRANCH})
    monkeypatch.setattr(client, "_patch_repo_admin", patch)
    try:
        result = await client.set_default_branch("owner", "repo", branch=NEW_BRANCH)
    finally:
        await client.aclose()

    assert result["default_branch"] == NEW_BRANCH
    patch.assert_awaited_once_with(
        "/repos/{owner}/{repo}",
        {"default_branch": NEW_BRANCH},
        owner="owner",
        repo="repo",
    )


@pytest.mark.asyncio
async def test_create_branch_at_sha_creates_and_verifies_exact_head(monkeypatch):
    client = FakeGovernanceClient()
    audit = _setup(monkeypatch, client)

    result = await remote.gitea_create_branch_at_sha("owner", "repo", NEW_BRANCH, NEW_SHA)

    assert result["ok"] is True
    assert result["result"]["created"] is True
    assert result["result"]["head_sha"] == NEW_SHA
    assert client.create_calls == [("owner", "repo", NEW_BRANCH, NEW_SHA)]
    assert client.branches[NEW_BRANCH] == NEW_SHA
    assert len(audit.required) == 1
    assert audit.required[0].action == "create_branch_at_sha"
    assert len(audit.best_effort) == 1


@pytest.mark.asyncio
async def test_create_branch_same_sha_is_idempotent_without_audit_or_mutation(monkeypatch):
    client = FakeGovernanceClient(branches={OLD_BRANCH: OLD_SHA, NEW_BRANCH: NEW_SHA})
    audit = _setup(monkeypatch, client)

    result = await remote.gitea_create_branch_at_sha("owner", "repo", NEW_BRANCH, NEW_SHA)

    assert result["ok"] is True
    assert result["result"]["created"] is False
    assert result["result"]["already_exists"] is True
    assert client.create_calls == []
    assert audit.required == []


@pytest.mark.asyncio
async def test_create_branch_never_overwrites_different_head(monkeypatch):
    client = FakeGovernanceClient(branches={OLD_BRANCH: OLD_SHA, NEW_BRANCH: OTHER_SHA})
    _setup(monkeypatch, client)

    result = await remote.gitea_create_branch_at_sha("owner", "repo", NEW_BRANCH, NEW_SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "BRANCH_EXISTS_DIFFERENT_HEAD"
    assert result["error"]["details"]["mutation_occurred"] is False
    assert client.create_calls == []


@pytest.mark.asyncio
async def test_create_branch_requires_source_sha_proof(monkeypatch):
    client = FakeGovernanceClient(commits={OLD_SHA})
    _setup(monkeypatch, client)

    result = await remote.gitea_create_branch_at_sha("owner", "repo", NEW_BRANCH, NEW_SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "SOURCE_SHA_UNPROVEN"
    assert client.create_calls == []


@pytest.mark.asyncio
async def test_create_branch_audit_failure_is_zero_mutation(monkeypatch):
    client = FakeGovernanceClient()
    audit = FakeAuditLogger(fail_required=True)
    _setup(monkeypatch, client, audit)

    result = await remote.gitea_create_branch_at_sha("owner", "repo", NEW_BRANCH, NEW_SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert client.create_calls == []
    assert NEW_BRANCH not in client.branches


@pytest.mark.asyncio
async def test_create_branch_transport_loss_reconciles_applied_mutation(monkeypatch):
    request = httpx.Request("POST", "https://git.example/api/v1/repos/owner/repo/branches")
    client = FakeGovernanceClient(
        create_error=httpx.ReadTimeout("lost response", request=request),
        apply_create=True,
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_create_branch_at_sha("owner", "repo", NEW_BRANCH, NEW_SHA)

    assert result["ok"] is True
    assert result["result"]["outcome"] == "created_after_ambiguous_response"
    assert client.create_calls == [("owner", "repo", NEW_BRANCH, NEW_SHA)]


@pytest.mark.asyncio
async def test_default_switch_verifies_both_target_and_current_default_leases(monkeypatch):
    client = FakeGovernanceClient(
        branches={OLD_BRANCH: OLD_SHA, NEW_BRANCH: NEW_SHA},
        default_branch=OLD_BRANCH,
    )
    audit = _setup(monkeypatch, client)

    result = await remote.gitea_set_default_branch(
        "owner",
        "repo",
        NEW_BRANCH,
        NEW_SHA,
        OLD_BRANCH,
        OLD_SHA,
    )

    assert result["ok"] is True
    assert result["result"]["changed"] is True
    assert result["result"]["default_branch"] == NEW_BRANCH
    assert client.default_branch == NEW_BRANCH
    assert client.default_calls == [("owner", "repo", NEW_BRANCH)]
    assert len(audit.required) == 1
    assert audit.required[0].action == "set_default_branch"


@pytest.mark.asyncio
async def test_default_switch_rejects_target_head_mismatch(monkeypatch):
    client = FakeGovernanceClient(branches={OLD_BRANCH: OLD_SHA, NEW_BRANCH: OTHER_SHA})
    _setup(monkeypatch, client)

    result = await remote.gitea_set_default_branch(
        "owner", "repo", NEW_BRANCH, NEW_SHA, OLD_BRANCH, OLD_SHA
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "TARGET_HEAD_MISMATCH"
    assert client.default_calls == []


@pytest.mark.asyncio
async def test_default_switch_rejects_current_default_name_mismatch(monkeypatch):
    client = FakeGovernanceClient(
        default_branch="other",
        branches={"other": OLD_SHA, OLD_BRANCH: OLD_SHA, NEW_BRANCH: NEW_SHA},
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_set_default_branch(
        "owner", "repo", NEW_BRANCH, NEW_SHA, OLD_BRANCH, OLD_SHA
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEFAULT_BRANCH_MISMATCH"
    assert client.default_calls == []


@pytest.mark.asyncio
async def test_default_switch_rejects_current_default_sha_mismatch(monkeypatch):
    client = FakeGovernanceClient(
        branches={OLD_BRANCH: OTHER_SHA, NEW_BRANCH: NEW_SHA},
        default_branch=OLD_BRANCH,
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_set_default_branch(
        "owner", "repo", NEW_BRANCH, NEW_SHA, OLD_BRANCH, OLD_SHA
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "CURRENT_DEFAULT_HEAD_MISMATCH"
    assert client.default_calls == []


@pytest.mark.asyncio
async def test_default_switch_same_proven_default_is_idempotent(monkeypatch):
    client = FakeGovernanceClient(
        default_branch=NEW_BRANCH,
        branches={NEW_BRANCH: NEW_SHA, OLD_BRANCH: OLD_SHA},
    )
    audit = _setup(monkeypatch, client)

    result = await remote.gitea_set_default_branch(
        "owner", "repo", NEW_BRANCH, NEW_SHA, OLD_BRANCH, OLD_SHA
    )

    assert result["ok"] is True
    assert result["result"]["changed"] is False
    assert result["result"]["already_default"] is True
    assert client.default_calls == []
    assert audit.required == []


@pytest.mark.asyncio
async def test_default_switch_audit_failure_is_zero_mutation(monkeypatch):
    client = FakeGovernanceClient(branches={OLD_BRANCH: OLD_SHA, NEW_BRANCH: NEW_SHA})
    audit = FakeAuditLogger(fail_required=True)
    _setup(monkeypatch, client, audit)

    result = await remote.gitea_set_default_branch(
        "owner", "repo", NEW_BRANCH, NEW_SHA, OLD_BRANCH, OLD_SHA
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert client.default_branch == OLD_BRANCH
    assert client.default_calls == []


@pytest.mark.asyncio
async def test_default_switch_transport_loss_reconciles_applied_mutation(monkeypatch):
    request = httpx.Request("PATCH", "https://git.example/api/v1/repos/owner/repo")
    client = FakeGovernanceClient(
        branches={OLD_BRANCH: OLD_SHA, NEW_BRANCH: NEW_SHA},
        default_error=httpx.ReadTimeout("lost response", request=request),
        apply_default=True,
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_set_default_branch(
        "owner", "repo", NEW_BRANCH, NEW_SHA, OLD_BRANCH, OLD_SHA
    )

    assert result["ok"] is True
    assert result["result"]["outcome"] == "changed_after_ambiguous_response"
    assert client.default_calls == [("owner", "repo", NEW_BRANCH)]


def test_governance_tools_are_write_admin_only():
    for tool in ("gitea_create_branch_at_sha", "gitea_set_default_branch"):
        assert tool in MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS
        assert tool not in TOOL_NAMES_BY_MODE["mcp_client"]
        assert tool in TOOL_NAMES_BY_MODE["mcp_client_write"]
        assert get_required_scopes(tool) == ["mcp:repo", "mcp:admin"]