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
        assert result["result"]["items"] == [{"number": 1, "title": "fix"}]
        assert result["result"]["count"] == 1
