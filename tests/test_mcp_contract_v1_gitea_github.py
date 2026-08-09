"""Contract v1 regression tests for gitea_*/github_* MCP tool wrappers.

Before this fix, every gitea_*/github_* tool used the legacy text_result()/
error_result() helpers (a {"content", "structuredContent", "_meta"} shape,
not the {"ok", "result", "error", "meta"} envelope every other tool uses)
and never caught exceptions from GiteaClient/GitHubClient at all -- a 404,
a bad token, or an invalid owner/repo propagated as an unhandled exception
instead of a clean tool_error(). gitea_get_repo/github_get_repo also
returned the raw API payload unfiltered, which embeds the repo owner's
email address -- unnecessary PII for a repo-metadata lookup.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

_MCP_SERVER_DIR = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server")
if _MCP_SERVER_DIR not in sys.path:
    sys.path.insert(0, _MCP_SERVER_DIR)

from examples.mcp_server import server as mcp_server_mod  # noqa: E402


def _assert_envelope(result: dict, *, ok: bool = True) -> None:
    assert result["ok"] is ok
    assert "result" in result
    assert "error" in result
    assert "meta" in result
    assert result["meta"]["contract_version"] == "1"


class _FakeRemoteClient:
    """Stand-in for GiteaClient/GitHubClient used as `async with Client(token) as c`."""

    def __init__(self, methods: dict[str, AsyncMock]) -> None:
        self._methods = methods
        for name, mock in methods.items():
            setattr(self, name, mock)

    async def __aenter__(self) -> _FakeRemoteClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


GITEA_REPO_PAYLOAD = {
    "id": 42,
    "owner": {
        "login": "gpakoh",
        "id": 1,
        "email": "gpakoh@example.com",
        "full_name": "Real Name",
        "avatar_url": "https://git.example.com/avatars/1",
    },
    "name": "web-ssh-gateway",
    "full_name": "gpakoh/web-ssh-gateway",
    "description": "SSH gateway",
    "private": False,
    "internal": False,
    "default_branch": "master",
    "permissions": {"admin": False, "push": False, "pull": True},
    "stars_count": 3,
    "forks_count": 1,
    "watchers_count": 2,
    "open_issues_count": 0,
    "topics": ["ssh", "gateway"],
    "archived": False,
    "html_url": "https://git.example.com/gpakoh/web-ssh-gateway",
}

GITHUB_REPO_PAYLOAD = {
    "id": 99,
    "owner": {"login": "gpakoh", "id": 2, "email": "gpakoh@example.com"},
    "name": "web-ssh-gateway",
    "full_name": "gpakoh/web-ssh-gateway",
    "description": "SSH gateway",
    "visibility": "public",
    "default_branch": "master",
    "permissions": {"admin": False, "maintain": False, "push": False, "triage": True, "pull": True},
    "stargazers_count": 5,
    "forks_count": 2,
    "watchers_count": 5,
    "open_issues_count": 1,
    "topics": ["ssh"],
    "archived": False,
    "html_url": "https://github.com/gpakoh/web-ssh-gateway",
}


class TestGiteaToolsContractV1:
    @pytest.mark.asyncio
    async def test_gitea_get_repo_success(self, monkeypatch):
        methods = {"get_repo": AsyncMock(return_value=dict(GITEA_REPO_PAYLOAD))}
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_get_repo("gpakoh", "web-ssh-gateway")
        _assert_envelope(result)
        assert result["meta"]["source"] == "gitea"
        assert result["result"]["owner"] == {"login": "gpakoh"}
        assert result["result"]["visibility"] == "public"
        assert result["result"]["default_branch"] == "master"
        assert result["result"]["counters"] == {
            "stars": 3,
            "forks": 1,
            "watchers": 2,
            "open_issues": 0,
        }
        import json

        assert "email" not in json.dumps(result)
        assert "Real Name" not in json.dumps(result)

    @pytest.mark.asyncio
    async def test_gitea_get_repo_missing_token_is_contract_v1_error(self, monkeypatch):
        monkeypatch.delenv("GITEA_TOKEN", raising=False)

        result = await mcp_server_mod.gitea_get_repo("gpakoh", "web-ssh-gateway")
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "DEPENDENCY_MISSING"

    @pytest.mark.asyncio
    async def test_gitea_get_repo_not_found_is_contract_v1_error(self, monkeypatch):
        request = httpx.Request("GET", "https://git.example.com/api/v1/repos/gpakoh/nope")
        response = httpx.Response(404, request=request)
        methods = {
            "get_repo": AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "gitea api /repos/gpakoh/nope: 404 Not Found",
                    request=request,
                    response=response,
                )
            )
        }
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_get_repo("gpakoh", "nope")
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "REMOTE_API_ERROR"

    @pytest.mark.asyncio
    async def test_gitea_get_repo_connection_failure_is_retryable(self, monkeypatch):
        """P2 audit finding: a transient network failure (Gitea briefly
        unreachable) used to be classified identically to an internal
        program defect -- INTERNAL_ERROR, retryable=False -- telling a
        calling agent not to bother retrying exactly the kind of
        condition a retry fixes."""
        methods = {
            "get_repo": AsyncMock(
                side_effect=httpx.ConnectError("All connection attempts failed")
            )
        }
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_get_repo("gpakoh", "web-ssh-gateway")
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "REMOTE_UNAVAILABLE"
        assert result["error"]["retryable"] is True

    @pytest.mark.asyncio
    async def test_gitea_get_repo_auth_failure_is_contract_v1_error(self, monkeypatch):
        methods = {"get_repo": AsyncMock(side_effect=PermissionError("gitea api ...: unauthorized"))}
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITEA_TOKEN", "bad-tok")

        result = await mcp_server_mod.gitea_get_repo("gpakoh", "web-ssh-gateway")
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "AUTH_ERROR"

    @pytest.mark.asyncio
    async def test_gitea_get_issue_invalid_input_is_contract_v1_error(self, monkeypatch):
        methods = {"get_issue": AsyncMock(side_effect=ValueError("Invalid owner: 'a/b'"))}
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_get_issue("a/b", "repo", 1)
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_gitea_list_branches_returns_structured_result(self, monkeypatch):
        methods = {
            "list_branches": AsyncMock(return_value=[{"name": "main"}, {"name": "dev"}])
        }
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_list_branches("gpakoh", "web-ssh-gateway")
        _assert_envelope(result)
        assert result["result"]["items"] == [{"name": "main"}, {"name": "dev"}]
        assert result["result"]["count"] == 2

    @pytest.mark.asyncio
    async def test_gitea_list_branches_rejects_negative_limit(self, monkeypatch):
        """P2 audit finding: a negative/zero limit used to be passed straight
        through to the remote API / list slicing instead of being rejected."""
        list_branches = AsyncMock(return_value=[{"name": "main"}])
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient({"list_branches": list_branches})
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_list_branches("gpakoh", "web-ssh-gateway", limit=-1)
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "INVALID_INPUT"
        list_branches.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gitea_list_branches_rejects_zero_limit(self, monkeypatch):
        list_branches = AsyncMock(return_value=[{"name": "main"}])
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient({"list_branches": list_branches})
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_list_branches("gpakoh", "web-ssh-gateway", limit=0)
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "INVALID_INPUT"
        list_branches.assert_not_awaited()


class TestGitHubToolsContractV1:
    @pytest.mark.asyncio
    async def test_github_get_repo_success(self, monkeypatch):
        methods = {"get_repo": AsyncMock(return_value=dict(GITHUB_REPO_PAYLOAD))}
        monkeypatch.setattr(
            mcp_server_mod, "GitHubClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        result = await mcp_server_mod.github_get_repo("gpakoh", "web-ssh-gateway")
        _assert_envelope(result)
        assert result["meta"]["source"] == "github"
        assert result["result"]["owner"] == {"login": "gpakoh"}
        assert result["result"]["counters"] == {
            "stars": 5,
            "forks": 2,
            "watchers": 5,
            "open_issues": 1,
        }
        import json

        assert "email" not in json.dumps(result)

    @pytest.mark.asyncio
    async def test_github_get_repo_missing_token_is_contract_v1_error(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        result = await mcp_server_mod.github_get_repo("gpakoh", "web-ssh-gateway")
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "DEPENDENCY_MISSING"

    @pytest.mark.asyncio
    async def test_github_list_pull_requests_returns_structured_result(self, monkeypatch):
        methods = {
            "list_pull_requests": AsyncMock(return_value=[{"number": 1, "title": "fix"}])
        }
        monkeypatch.setattr(
            mcp_server_mod, "GitHubClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        result = await mcp_server_mod.github_list_pull_requests("gpakoh", "web-ssh-gateway")
        _assert_envelope(result)
        assert result["result"]["items"] == [
            {
                "number": 1,
                "title": "fix",
                "state": None,
                "user": None,
                "labels": [],
                "created_at": None,
                "updated_at": None,
                "body_truncated": False,
            }
        ]
        assert result["result"]["count"] == 1


GITEA_ISSUE_PAYLOAD = {
    "id": 42,
    "number": 42,
    "title": "Bug: crash on startup",
    "body": "Repro steps...",
    "state": "open",
    "user": {
        "login": "gpakoh",
        "id": 1,
        "email": "gpakoh@example.com",
        "full_name": "Real Name",
        "is_admin": True,
        "last_login": "2026-08-01T10:00:00Z",
        "created": "2020-01-01T00:00:00Z",
        "restricted": False,
        "active": True,
    },
    "assignees": [
        {
            "login": "alice",
            "id": 2,
            "email": "alice@example.com",
            "full_name": "Alice",
            "is_admin": False,
            "last_login": "2026-07-01T10:00:00Z",
        }
    ],
    "labels": [
        {
            "id": 7,
            "name": "bug",
            "color": "d73a4a",
            "description": "Something is broken",
            "url": "https://git.example.com/api/v1/labels/7",
        }
    ],
    "milestone": {
        "id": 3,
        "title": "v1.1",
        "state": "open",
        "due_on": "2026-09-01T00:00:00Z",
        "creator": {
            "login": "carol",
            "email": "carol@example.com",
            "is_admin": True,
            "last_login": "2026-06-01T10:00:00Z",
        },
    },
    "comments": 3,
    "created_at": "2026-07-01T08:00:00Z",
    "updated_at": "2026-07-02T09:00:00Z",
    "closed_at": None,
    "html_url": "https://git.example.com/gpakoh/web-ssh-gateway/issues/42",
    "api_url": "https://git.example.com/api/v1/repos/gpakoh/web-ssh-gateway/issues/42",
}


class TestPaginationValidation:
    """P2 audit finding: negative/zero per_page/limit arguments were
    accepted and passed straight through instead of being rejected."""

    @pytest.mark.asyncio
    async def test_github_list_branches_rejects_negative_per_page(self, monkeypatch):
        list_branches = AsyncMock(return_value=[{"name": "main"}])
        monkeypatch.setattr(
            mcp_server_mod, "GitHubClient", lambda token: _FakeRemoteClient({"list_branches": list_branches})
        )
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        result = await mcp_server_mod.github_list_branches("gpakoh", "web-ssh-gateway", per_page=-1)
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "INVALID_INPUT"
        list_branches.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_github_list_branches_rejects_zero_per_page(self, monkeypatch):
        list_branches = AsyncMock(return_value=[{"name": "main"}])
        monkeypatch.setattr(
            mcp_server_mod, "GitHubClient", lambda token: _FakeRemoteClient({"list_branches": list_branches})
        )
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        result = await mcp_server_mod.github_list_branches("gpakoh", "web-ssh-gateway", per_page=0)
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "INVALID_INPUT"
        list_branches.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_github_list_branches_rejects_excessive_per_page(self, monkeypatch):
        list_branches = AsyncMock(return_value=[{"name": "main"}])
        monkeypatch.setattr(
            mcp_server_mod, "GitHubClient", lambda token: _FakeRemoteClient({"list_branches": list_branches})
        )
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        result = await mcp_server_mod.github_list_branches("gpakoh", "web-ssh-gateway", per_page=100_000)
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "INVALID_INPUT"
        list_branches.assert_not_awaited()


class TestIssuePayloadMinimization:
    """Regression: gitea_*/github_* issue tools must not leak user PII.

    Gitea issue payloads embed full user objects (email, is_admin,
    last_login, created, restricted, ...) on the issue author, assignees,
    and milestone creator. The tool boundary must strip these before the
    payload reaches the agent (audit finding #6).
    """

    @pytest.mark.asyncio
    async def test_gitea_list_issues_strips_user_pii(self, monkeypatch):
        methods = {"list_issues": AsyncMock(return_value=[dict(GITEA_ISSUE_PAYLOAD)])}
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_list_issues("gpakoh", "web-ssh-gateway")
        _assert_envelope(result)
        item = result["result"]["items"][0]
        assert item["number"] == 42
        assert item["title"] == "Bug: crash on startup"
        assert item["state"] == "open"
        assert item["user"] == {"login": "gpakoh", "full_name": "Real Name"}
        assert item["assignees"] == [{"login": "alice", "full_name": "Alice"}]
        assert item["labels"] == [{"name": "bug", "color": "d73a4a"}]
        assert item["milestone"] == {
            "title": "v1.1",
            "state": "open",
            "due_on": "2026-09-01T00:00:00Z",
        }
        import json

        serialized = json.dumps(result)
        assert "email" not in serialized
        assert "is_admin" not in serialized
        assert "last_login" not in serialized
        assert "restricted" not in serialized
        assert "api_url" not in serialized

    @pytest.mark.asyncio
    async def test_gitea_get_issue_strips_user_pii(self, monkeypatch):
        methods = {"get_issue": AsyncMock(return_value=dict(GITEA_ISSUE_PAYLOAD))}
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_get_issue("gpakoh", "web-ssh-gateway", 42)
        _assert_envelope(result)
        import json

        serialized = json.dumps(result)
        assert "email" not in serialized
        assert "is_admin" not in serialized
        assert "last_login" not in serialized
        assert result["result"]["user"] == {"login": "gpakoh", "full_name": "Real Name"}

    @pytest.mark.asyncio
    async def test_gitea_list_pull_requests_strips_user_pii(self, monkeypatch):
        pr = dict(GITEA_ISSUE_PAYLOAD)
        pr["pull_request"] = {"url": "https://git.example.com/api/v1/pulls/42"}
        pr["head"] = {"label": "gpakoh:fix", "ref": "fix", "sha": "abc123"}
        pr["base"] = {"label": "gpakoh:master", "ref": "master", "sha": "def456"}
        methods = {"list_pull_requests": AsyncMock(return_value=[pr])}
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_list_pull_requests("gpakoh", "web-ssh-gateway")
        _assert_envelope(result)
        item = result["result"]["items"][0]
        assert item["pull_request"] is True
        assert item["head"] == {"label": "gpakoh:fix", "ref": "fix", "sha": "abc123"}
        assert item["base"] == {"label": "gpakoh:master", "ref": "master", "sha": "def456"}
        import json

        serialized = json.dumps(result)
        assert "email" not in serialized
        assert "is_admin" not in serialized
        assert "last_login" not in serialized

    @pytest.mark.asyncio
    async def test_github_list_issues_strips_user_pii(self, monkeypatch):
        gh_issue = dict(GITEA_ISSUE_PAYLOAD)
        gh_issue["user"] = {
            "login": "gpakoh",
            "id": 2,
            "email": "gpakoh@example.com",
            "node_id": "MDQ6VXNlcjI=",
        }
        methods = {"list_issues": AsyncMock(return_value=[gh_issue])}
        monkeypatch.setattr(
            mcp_server_mod, "GitHubClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        result = await mcp_server_mod.github_list_issues("gpakoh", "web-ssh-gateway")
        _assert_envelope(result)
        import json

        serialized = json.dumps(result)
        assert "email" not in serialized
        assert "node_id" not in serialized
        assert result["result"]["items"][0]["user"] == {"login": "gpakoh"}

    @pytest.mark.asyncio
    async def test_github_get_pull_request_strips_user_pii(self, monkeypatch):
        pr = dict(GITEA_ISSUE_PAYLOAD)
        pr["pull_request"] = {"url": "https://api.github.com/pulls/42"}
        pr["draft"] = True
        methods = {"get_pull_request": AsyncMock(return_value=pr)}
        monkeypatch.setattr(
            mcp_server_mod, "GitHubClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        result = await mcp_server_mod.github_get_pull_request("gpakoh", "web-ssh-gateway", 42)
        _assert_envelope(result)
        import json

        serialized = json.dumps(result)
        assert "email" not in serialized
        assert "is_admin" not in serialized
        assert "last_login" not in serialized
        assert result["result"]["draft"] is True

    @pytest.mark.asyncio
    async def test_gitea_list_issues_truncates_huge_body(self, monkeypatch):
        issue = dict(GITEA_ISSUE_PAYLOAD)
        issue["body"] = "Dependabot dependency update " * 500
        methods = {"list_issues": AsyncMock(return_value=[issue])}
        monkeypatch.setattr(
            mcp_server_mod, "GiteaClient", lambda token: _FakeRemoteClient(methods)
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

        result = await mcp_server_mod.gitea_list_issues("gpakoh", "web-ssh-gateway")
        _assert_envelope(result)
        item = result["result"]["items"][0]
        assert item["body_truncated"] is True
        assert len(item["body"]) < 6000
        assert "truncated" in item["body"]
