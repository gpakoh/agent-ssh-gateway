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
PROTECTION_NAME = "release/*"
_DEFAULT_PROTECTION = object()


def _protection(**overrides) -> dict[str, object]:
    state: dict[str, object] = {
        "rule_name": PROTECTION_NAME,
        "branch_name": PROTECTION_NAME,
        "enable_status_check": True,
        "status_check_contexts": ["ci"],
        "required_approvals": 1,
        "block_on_outdated_branch": True,
        "block_on_rejected_reviews": True,
        "block_admin_merge_override": True,
    }
    state.update(overrides)
    return state


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
        protection: dict[str, object] | None | object = _DEFAULT_PROTECTION,
        protection_error: BaseException | None = None,
        delete_protection_error: BaseException | None = None,
        apply_create: bool = True,
        apply_default: bool = True,
        apply_protection: bool = True,
        apply_delete_protection: bool = True,
    ) -> None:
        self.default_branch = default_branch
        self.branches = branches or {OLD_BRANCH: OLD_SHA}
        self.commits = commits or {OLD_SHA, NEW_SHA}
        self.permissions = permissions or {"push": True, "admin": True}
        self.create_error = create_error
        self.default_error = default_error
        if protection is _DEFAULT_PROTECTION:
            self.protection: dict[str, object] | None = _protection()
        elif protection is None:
            self.protection = None
        else:
            assert isinstance(protection, dict)
            self.protection = dict(protection)
        self.protection_error = protection_error
        self.delete_protection_error = delete_protection_error
        self.apply_create = apply_create
        self.apply_default = apply_default
        self.apply_protection = apply_protection
        self.apply_delete_protection = apply_delete_protection
        self.create_calls: list[tuple[str, str, str, str]] = []
        self.default_calls: list[tuple[str, str, str]] = []
        self.protection_create_calls: list[tuple[str, str, str, dict[str, object]]] = []
        self.protection_update_calls: list[tuple[str, str, str, dict[str, object]]] = []
        self.protection_delete_calls: list[tuple[str, str, str]] = []

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

    async def get_branch_protection(self, owner: str, repo: str, *, name: str):
        assert name == PROTECTION_NAME
        return None if self.protection is None else dict(self.protection)

    async def create_branch_protection(
        self,
        owner: str,
        repo: str,
        *,
        name: str,
        payload: dict[str, object],
    ):
        self.protection_create_calls.append((owner, repo, name, dict(payload)))
        if self.apply_protection:
            self.protection = {
                "rule_name": name,
                "branch_name": name,
                **payload,
            }
        if self.protection_error is not None:
            raise self.protection_error
        return None if self.protection is None else dict(self.protection)

    async def update_branch_protection(
        self,
        owner: str,
        repo: str,
        *,
        name: str,
        payload: dict[str, object],
    ):
        self.protection_update_calls.append((owner, repo, name, dict(payload)))
        if self.apply_protection and self.protection is not None:
            self.protection.update(payload)
        if self.protection_error is not None:
            raise self.protection_error
        return None if self.protection is None else dict(self.protection)

    async def delete_branch_protection(self, owner: str, repo: str, *, name: str):
        self.protection_delete_calls.append((owner, repo, name))
        was_present = self.protection is not None
        if self.apply_delete_protection:
            self.protection = None
        if self.delete_protection_error is not None:
            raise self.delete_protection_error
        return was_present


def _setup(monkeypatch, client: FakeGovernanceClient, audit: FakeAuditLogger | None = None) -> FakeAuditLogger:
    logger = audit or FakeAuditLogger()
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    monkeypatch.setattr(remote, "_caller_fingerprint", lambda: "caller-fingerprint")
    monkeypatch.setattr(remote, "_get_gitea_audit_logger", lambda: logger)
    return logger


def _protection_state_sha(protection: dict[str, object] | None) -> str:
    return remote._branch_protection_state_sha256(
        remote._branch_protection_state(protection)
    )


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
async def test_client_branch_protection_uses_dedicated_lifecycle_endpoints(monkeypatch):
    client = GiteaClient("token")
    get = AsyncMock(return_value=_protection())
    post = AsyncMock(return_value=_protection(required_approvals=2))
    patch = AsyncMock(return_value=_protection(required_approvals=3))
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(client, "_get_optional_branch_protection", get)
    monkeypatch.setattr(client, "_post_branch_protection", post)
    monkeypatch.setattr(client, "_patch_branch_protection", patch)
    monkeypatch.setattr(client, "_delete_branch_protection", delete)
    try:
        observed = await client.get_branch_protection(
            "owner", "repo", name=PROTECTION_NAME
        )
        created = await client.create_branch_protection(
            "owner",
            "repo",
            name=PROTECTION_NAME,
            payload={"required_approvals": 2},
        )
        updated = await client.update_branch_protection(
            "owner",
            "repo",
            name=PROTECTION_NAME,
            payload={"required_approvals": 3},
        )
        deleted = await client.delete_branch_protection(
            "owner", "repo", name=PROTECTION_NAME
        )
    finally:
        await client.aclose()

    assert observed is not None and observed["required_approvals"] == 1
    assert created["required_approvals"] == 2
    assert updated["required_approvals"] == 3
    assert deleted is True
    get.assert_awaited_once_with(
        "/repos/{owner}/{repo}/branch_protections/{name}",
        owner="owner",
        repo="repo",
        name=PROTECTION_NAME,
    )
    post.assert_awaited_once_with(
        "/repos/{owner}/{repo}/branch_protections",
        {
            "rule_name": PROTECTION_NAME,
            "branch_name": PROTECTION_NAME,
            "required_approvals": 2,
        },
        owner="owner",
        repo="repo",
    )
    patch.assert_awaited_once_with(
        "/repos/{owner}/{repo}/branch_protections/{name}",
        {"required_approvals": 3},
        owner="owner",
        repo="repo",
        name=PROTECTION_NAME,
    )
    delete.assert_awaited_once_with(
        "/repos/{owner}/{repo}/branch_protections/{name}",
        owner="owner",
        repo="repo",
        name=PROTECTION_NAME,
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


@pytest.mark.asyncio
async def test_branch_protection_read_fingerprints_absence(monkeypatch):
    client = FakeGovernanceClient(protection=None)
    _setup(monkeypatch, client)

    result = await remote.gitea_get_branch_protection(
        "owner", "repo", PROTECTION_NAME
    )

    assert result["ok"] is True
    assert result["result"]["state"] == {"exists": False, "protection": None}
    assert result["result"]["state_sha256"] == _protection_state_sha(None)


@pytest.mark.asyncio
async def test_branch_protection_expected_absence_creates_and_verifies(monkeypatch):
    client = FakeGovernanceClient(protection=None)
    audit = _setup(monkeypatch, client)
    expected = _protection_state_sha(None)

    result = await remote.gitea_set_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        expected,
        True,
        ["security", "ci"],
        2,
        True,
        True,
        False,
        "create guarded release protection",
    )

    assert result["ok"] is True
    assert result["result"]["created"] is True
    assert result["result"]["changed"] is True
    assert result["result"]["state"]["exists"] is True
    protection = result["result"]["state"]["protection"]
    assert protection["status_check_contexts"] == ["ci", "security"]
    assert protection["required_approvals"] == 2
    assert len(client.protection_create_calls) == 1
    assert client.protection_update_calls == []
    assert len(audit.required) == 1
    assert audit.required[0].action == "set_branch_protection"
    assert len(audit.best_effort) == 1


@pytest.mark.asyncio
async def test_branch_protection_existing_rule_updates_under_exact_lease(monkeypatch):
    client = FakeGovernanceClient()
    audit = _setup(monkeypatch, client)
    expected = _protection_state_sha(client.protection)

    result = await remote.gitea_set_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        expected,
        True,
        ["ci", "security"],
        2,
        True,
        True,
        False,
        "tighten release protection",
    )

    assert result["ok"] is True
    assert result["result"]["created"] is False
    assert result["result"]["changed"] is True
    assert client.protection_create_calls == []
    assert len(client.protection_update_calls) == 1
    assert len(audit.required) == 1
    assert len(audit.best_effort) == 1


@pytest.mark.asyncio
async def test_branch_protection_stale_fingerprint_is_zero_mutation(monkeypatch):
    client = FakeGovernanceClient()
    audit = _setup(monkeypatch, client)

    result = await remote.gitea_set_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        "0" * 64,
        True,
        ["ci", "security"],
        2,
        True,
        True,
        False,
        "stale lease must fail",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "PROTECTION_STATE_MISMATCH"
    assert result["error"]["details"]["mutation_occurred"] is False
    assert client.protection_create_calls == []
    assert client.protection_update_calls == []
    assert audit.required == []


@pytest.mark.asyncio
async def test_branch_protection_idempotent_state_is_zero_mutation(monkeypatch):
    client = FakeGovernanceClient()
    audit = _setup(monkeypatch, client)
    expected = _protection_state_sha(client.protection)

    result = await remote.gitea_set_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        expected,
        True,
        ["ci"],
        1,
        True,
        True,
        True,
        "already desired",
    )

    assert result["ok"] is True
    assert result["result"]["changed"] is False
    assert result["result"]["already_desired"] is True
    assert client.protection_create_calls == []
    assert client.protection_update_calls == []
    assert audit.required == []


@pytest.mark.asyncio
async def test_branch_protection_audit_failure_is_zero_mutation(monkeypatch):
    client = FakeGovernanceClient(protection=None)
    audit = FakeAuditLogger(fail_required=True)
    _setup(monkeypatch, client, audit)

    result = await remote.gitea_set_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        _protection_state_sha(None),
        True,
        ["ci"],
        1,
        True,
        True,
        True,
        "audit must persist before create",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert client.protection is None
    assert client.protection_create_calls == []


@pytest.mark.asyncio
async def test_branch_protection_rejects_duplicate_contexts_before_mutation(monkeypatch):
    client = FakeGovernanceClient()
    _setup(monkeypatch, client)

    result = await remote.gitea_set_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        _protection_state_sha(client.protection),
        True,
        ["ci", "ci"],
        1,
        True,
        True,
        True,
        "duplicate context invalid",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert client.protection_create_calls == []
    assert client.protection_update_calls == []


@pytest.mark.asyncio
async def test_branch_protection_transport_loss_after_create_reconciles(monkeypatch):
    request = httpx.Request(
        "POST", "https://git.example/api/v1/repos/owner/repo/branch_protections"
    )
    client = FakeGovernanceClient(
        protection=None,
        protection_error=httpx.ReadTimeout("lost response", request=request),
        apply_protection=True,
    )
    _setup(monkeypatch, client)

    result = await remote.gitea_set_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        _protection_state_sha(None),
        True,
        ["ci", "security"],
        2,
        True,
        True,
        False,
        "reconcile lost create response",
    )

    assert result["ok"] is True
    assert result["result"]["outcome"] == "changed_after_ambiguous_response"
    assert len(client.protection_create_calls) == 1


@pytest.mark.asyncio
async def test_branch_protection_transport_loss_without_update_is_unknown(monkeypatch):
    request = httpx.Request(
        "PATCH",
        "https://git.example/api/v1/repos/owner/repo/branch_protections/release",
    )
    client = FakeGovernanceClient(
        protection_error=httpx.ReadTimeout("lost response", request=request),
        apply_protection=False,
    )
    _setup(monkeypatch, client)
    expected = _protection_state_sha(client.protection)

    result = await remote.gitea_set_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        expected,
        True,
        ["ci", "security"],
        2,
        True,
        True,
        False,
        "unknown update response",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert len(client.protection_update_calls) == 1


@pytest.mark.asyncio
async def test_branch_protection_delete_exact_lease_and_verify_absence(monkeypatch):
    client = FakeGovernanceClient()
    audit = _setup(monkeypatch, client)
    expected = _protection_state_sha(client.protection)

    result = await remote.gitea_delete_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        expected,
        "remove temporary acceptance protection",
    )

    assert result["ok"] is True
    assert result["result"]["deleted"] is True
    assert result["result"]["state"] == {"exists": False, "protection": None}
    assert client.protection is None
    assert client.protection_delete_calls == [("owner", "repo", PROTECTION_NAME)]
    assert len(audit.required) == 1
    assert audit.required[0].action == "delete_branch_protection"
    assert len(audit.best_effort) == 1


@pytest.mark.asyncio
async def test_branch_protection_delete_absent_is_idempotent(monkeypatch):
    client = FakeGovernanceClient(protection=None)
    audit = _setup(monkeypatch, client)

    result = await remote.gitea_delete_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        _protection_state_sha(None),
        "confirm already absent",
    )

    assert result["ok"] is True
    assert result["result"]["deleted"] is False
    assert result["result"]["already_absent"] is True
    assert client.protection_delete_calls == []
    assert audit.required == []


@pytest.mark.asyncio
async def test_branch_protection_delete_transport_loss_reconciles_absence(monkeypatch):
    request = httpx.Request(
        "DELETE",
        "https://git.example/api/v1/repos/owner/repo/branch_protections/release",
    )
    client = FakeGovernanceClient(
        delete_protection_error=httpx.ReadTimeout("lost response", request=request),
        apply_delete_protection=True,
    )
    _setup(monkeypatch, client)
    expected = _protection_state_sha(client.protection)

    result = await remote.gitea_delete_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        expected,
        "reconcile lost delete response",
    )

    assert result["ok"] is True
    assert result["result"]["outcome"] == "deleted_after_ambiguous_response"
    assert len(client.protection_delete_calls) == 1


@pytest.mark.asyncio
async def test_branch_protection_delete_transport_loss_without_delete_is_unknown(monkeypatch):
    request = httpx.Request(
        "DELETE",
        "https://git.example/api/v1/repos/owner/repo/branch_protections/release",
    )
    client = FakeGovernanceClient(
        delete_protection_error=httpx.ReadTimeout("lost response", request=request),
        apply_delete_protection=False,
    )
    _setup(monkeypatch, client)
    expected = _protection_state_sha(client.protection)

    result = await remote.gitea_delete_branch_protection(
        "owner",
        "repo",
        PROTECTION_NAME,
        expected,
        "unknown delete response",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert len(client.protection_delete_calls) == 1


def test_governance_tools_are_write_admin_only():
    for tool in (
        "gitea_create_branch_at_sha",
        "gitea_set_default_branch",
        "gitea_set_branch_protection",
        "gitea_delete_branch_protection",
    ):
        assert tool in MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS
        assert tool not in TOOL_NAMES_BY_MODE["mcp_client"]
        assert tool in TOOL_NAMES_BY_MODE["mcp_client_write"]
        assert get_required_scopes(tool) == ["mcp:repo", "mcp:admin"]

    assert "gitea_get_branch_protection" not in MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS
    assert "gitea_get_branch_protection" in TOOL_NAMES_BY_MODE["mcp_client"]
    assert "gitea_get_branch_protection" in TOOL_NAMES_BY_MODE["mcp_client_write"]
    assert get_required_scopes("gitea_get_branch_protection") == ["mcp:repo"]
