"""Contract v1 regression tests for independently deployed fleet adapters."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

_MCP_CLIENT_REMOTE = Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"
sys.path.insert(0, str(_MCP_CLIENT_REMOTE))
sys.path.insert(0, str(_MCP_CLIENT_REMOTE / "fleet"))

os.environ.setdefault("PGHOST", "127.0.0.1")
os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "example_vectordb")
os.environ.setdefault("PGUSER", "mcp_readonly")
os.environ.setdefault("PGPASSWORD", "test123")


class _AsyncClient:
    def __init__(self, **responses: Any) -> None:
        self.responses = responses

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        response = self.responses["repo"]
        if isinstance(response, Exception):
            raise response
        return response

    async def ps(self, **kwargs: Any) -> list[dict[str, Any]]:
        response = self.responses["ps"]
        if isinstance(response, Exception):
            raise response
        return response

    async def execute(self, sql: str) -> list[dict[str, Any]]:
        response = self.responses["select"]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_gitea_repo_is_minimized_and_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.gitea_server as server

    raw = {
        "owner": {"login": "alice", "email": "private@example.test"},
        "name": "repo",
        "full_name": "alice/repo",
        "description": "demo",
        "private": True,
        "language": "Python",
        "default_branch": "master",
        "permissions": {"pull": True},
        "stars_count": 2,
        "forks_count": 1,
        "watchers_count": 3,
        "open_issues_count": 4,
        "topics": ["mcp"],
        "archived": False,
        "html_url": "https://git.example/alice/repo",
        "api_url": "https://git.example/api/v1/repos/alice/repo",
    }
    monkeypatch.setattr(server, "_get_client", lambda: _AsyncClient(repo=raw))

    result = await server.gitea_get_repo("alice", "repo")

    assert result["ok"] is True
    assert result["tool"] == "gitea_get_repo"
    assert result["meta"]["contract_version"] == "1"
    assert result["result"]["owner"] == {"login": "alice"}
    assert "private@example.test" not in str(result)
    assert "api_url" not in result["result"]


@pytest.mark.asyncio
async def test_docker_ps_has_named_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.docker_server as server

    rows = [{"Names": "web", "Status": "Up"}]
    monkeypatch.setattr(server, "_get_client", lambda: _AsyncClient(ps=rows))

    result = await server.docker_ps()

    assert result["ok"] is True
    assert result["result"] == {"containers": rows, "count": 1}
    assert result["meta"]["source"] == "docker"


@pytest.mark.asyncio
async def test_postgres_select_returns_rows_not_json_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fleet.postgres_server as server

    rows = [{"answer": 42}]

    async def get_client() -> _AsyncClient:
        return _AsyncClient(select=rows)

    monkeypatch.setattr(server, "_get_client", get_client)

    result = await server.postgres_select("SELECT 42 AS answer")

    assert result["ok"] is True
    assert result["result"] == {"rows": rows, "row_count": 1}
    assert isinstance(result["result"]["rows"], list)


@pytest.mark.asyncio
async def test_postgres_rejection_is_error_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.postgres_server as server

    async def get_client() -> _AsyncClient:
        return _AsyncClient(select=ValueError("Only SELECT and WITH queries are allowed"))

    monkeypatch.setattr(server, "_get_client", get_client)

    result = await server.postgres_select("DELETE FROM users")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False


def test_install_contract_tool_wrapper_removed() -> None:
    """Regression: a generic mcp.tool()-patching wrapper (install_contract_
    tool_wrapper) briefly existed in shared.py, blanket-mapping every
    uncaught exception (including a plain HTTP 404) to UPSTREAM_ERROR with
    retryable=True. Replaced with per-function tool_success/tool_error
    calls plus the shared remote_api_error() classifier -- the wrapper
    itself must not come back.
    """
    import fleet.shared as shared

    assert not hasattr(shared, "install_contract_tool_wrapper")


@pytest.mark.asyncio
async def test_gitea_repo_not_found_is_remote_api_error_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the removed generic wrapper mapped ANY uncaught
    exception -- including a 404 from a typo'd repo name, an everyday
    mistake -- to UPSTREAM_ERROR/retryable=True. A 404 is not retryable.
    """
    import fleet.gitea_server as server
    import httpx

    request = httpx.Request("GET", "https://git.example/api/v1/repos/alice/nope")
    response = httpx.Response(404, request=request)
    not_found = httpx.HTTPStatusError(
        "gitea api /repos/alice/nope: 404 Not Found", request=request, response=response
    )
    monkeypatch.setattr(server, "_get_client", lambda: _AsyncClient(repo=not_found))

    result = await server.gitea_get_repo("alice", "nope")

    assert result["ok"] is False
    assert result["error"]["code"] == "REMOTE_API_ERROR"
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_docker_ps_command_failure_is_docker_command_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: same wrapper-removal fix on the docker adapter -- a
    docker CLI failure (RuntimeError from DockerClient._run) must map to
    DOCKER_COMMAND_FAILED, not the removed wrapper's generic UPSTREAM_ERROR.
    """
    import fleet.docker_server as server

    monkeypatch.setattr(
        server, "_get_client", lambda: _AsyncClient(ps=RuntimeError("docker exited 1: no such host"))
    )

    result = await server.docker_ps()

    assert result["ok"] is False
    assert result["error"]["code"] == "DOCKER_COMMAND_FAILED"
