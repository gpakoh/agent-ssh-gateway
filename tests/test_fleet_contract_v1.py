"""Contract v1 regression tests for independently deployed fleet adapters."""

from __future__ import annotations

import inspect
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
async def test_docker_ps_meta_reports_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """meta.redacted/truncated must reflect what ps() actually did: rows are
    sanitized (last_redacted) and possibly cut (last_truncated)."""
    import fleet.docker_server as server

    rows = [{"Names": "web", "Status": "Up"}]
    client = _AsyncClient(ps=rows)
    client.last_redacted = True
    client.last_truncated = False
    monkeypatch.setattr(server, "_get_client", lambda: client)

    result = await server.docker_ps()

    assert result["meta"]["redacted"] is True
    assert result["meta"]["truncated"] is False


@pytest.mark.asyncio
async def test_main_server_docker_ps_meta_reports_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import server as main_server

    class _FakeDC:
        last_redacted = True
        last_truncated = False

        async def ps(self, all: bool = False, limit: int = 50) -> list[dict]:
            return [{"Names": "web"}]

    monkeypatch.setattr(main_server, "DockerClient", lambda: _FakeDC())

    result = await main_server.docker_ps()

    assert result["ok"] is True
    assert result["meta"]["redacted"] is True
    assert result["meta"]["truncated"] is False


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
async def test_gitea_connection_failure_is_remote_unavailable_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 audit finding: remote_api_error()'s own docstring already called
    out "unlike a real transport failure" as a distinct, retryable case,
    but never actually implemented it -- httpx.TransportError (Gitea/
    GitHub briefly unreachable, DNS failure, connection refused) fell
    through to the generic INTERNAL_ERROR/retryable=False branch, same as
    an actual internal defect.
    """
    import fleet.gitea_server as server
    import httpx

    unreachable = httpx.ConnectError("All connection attempts failed")
    monkeypatch.setattr(server, "_get_client", lambda: _AsyncClient(repo=unreachable))

    result = await server.gitea_get_repo("alice", "repo")

    assert result["ok"] is False
    assert result["error"]["code"] == "REMOTE_UNAVAILABLE"
    assert result["error"]["retryable"] is True


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


@pytest.mark.asyncio
async def test_docker_ps_rejects_custom_go_template_format() -> None:
    """Regression (audit P0-1): the MCP docker_ps tool must not accept an
    arbitrary Go-template format. docker_ps(format="{{.Labels}}") used to
    return raw docker output with unredacted absolute host paths, bypassing
    the row sanitizer, the limit and the truncation metadata entirely."""
    import fleet.docker_server as server

    assert "format" not in inspect.signature(server.docker_ps).parameters
    with pytest.raises(TypeError):
        await server.docker_ps(all=False, format="{{.Labels}}", limit=3)


@pytest.mark.asyncio
async def test_docker_images_rejects_custom_go_template_format() -> None:
    """Same bypass existed on docker images --format; the tool must not
    expose it either."""
    import fleet.docker_server as server

    assert "format" not in inspect.signature(server.docker_images).parameters
    with pytest.raises(TypeError):
        await server.docker_images(format="{{.Labels}}")


@pytest.mark.asyncio
async def test_docker_stats_rejects_custom_go_template_format() -> None:
    """Same bypass existed on docker stats --format; the tool must not
    expose it either."""
    import fleet.docker_server as server

    assert "format" not in inspect.signature(server.docker_stats).parameters
    with pytest.raises(TypeError):
        await server.docker_stats(format="{{.Labels}}")


@pytest.mark.asyncio
async def test_main_server_docker_tools_reject_custom_go_template_format() -> None:
    """Audit P0-1 also applied to examples/mcp_server/server.py, which had
    its own docker_ps/images/stats tools forwarding an arbitrary Go-template
    format to the client. The main server must not expose it either."""
    from examples.mcp_server import server as main_server

    assert "format" not in inspect.signature(main_server.docker_ps).parameters
    assert "format" not in inspect.signature(main_server.docker_images).parameters
    assert "format" not in inspect.signature(main_server.docker_stats).parameters
    with pytest.raises(TypeError):
        await main_server.docker_ps(all=False, format="{{.Labels}}", limit=3)
    with pytest.raises(TypeError):
        await main_server.docker_images(format="{{.Labels}}")
    with pytest.raises(TypeError):
        await main_server.docker_stats(format="{{.Labels}}")


GITEA_RAW_ISSUE = {
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
            "url": "https://git.example/api/v1/labels/7",
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
    "html_url": "https://git.example/gpakoh/repo/issues/42",
    "api_url": "https://git.example/api/v1/repos/gpakoh/repo/issues/42",
}


class _IssueClient:
    """Fleet-client stand-in exposing the issue/PR methods."""

    def __init__(self, *, issues=None, issue=None, prs=None, pr=None) -> None:
        self._issues = issues or []
        self._issue = issue
        self._prs = prs or []
        self._pr = pr

    async def __aenter__(self) -> _IssueClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def list_issues(self, owner: str, repo: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._issues

    async def get_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        return self._issue

    async def list_pull_requests(self, owner: str, repo: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._prs

    async def get_pull_request(self, owner: str, repo: str, pull_number: int) -> dict[str, Any]:
        return self._pr


@pytest.mark.asyncio
async def test_fleet_gitea_list_issues_strips_user_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.gitea_server as server

    monkeypatch.setattr(
        server, "_get_client", lambda: _IssueClient(issues=[dict(GITEA_RAW_ISSUE)])
    )

    result = await server.gitea_list_issues("gpakoh", "repo")

    assert result["ok"] is True
    item = result["result"]["items"][0]
    assert item["number"] == 42
    assert item["user"] == {"login": "gpakoh", "full_name": "Real Name"}
    assert item["assignees"] == [{"login": "alice"}]
    assert item["labels"] == [{"name": "bug", "color": "d73a4a"}]
    assert item["milestone"] == {"title": "v1.1", "state": "open", "due_on": "2026-09-01T00:00:00Z"}
    assert "email" not in str(result)
    assert "is_admin" not in str(result)
    assert "last_login" not in str(result)
    assert "api_url" not in result["result"]


@pytest.mark.asyncio
async def test_fleet_gitea_list_issues_carries_pagination_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (audit item 6): list tools must expose pagination metadata
    (per_page, page, reliable truncated) instead of a bare wrapped array."""
    import fleet.gitea_server as server

    monkeypatch.setattr(
        server,
        "_get_client",
        lambda: _IssueClient(issues=[dict(GITEA_RAW_ISSUE) for _ in range(30)]),
    )

    result = await server.gitea_list_issues("gpakoh", "repo")

    assert result["ok"] is True
    meta = result["result"]
    assert meta["per_page"] == 30
    assert meta["page"] == 1
    assert meta["truncated"] is True  # fetched exactly the page size


@pytest.mark.asyncio
async def test_fleet_gitea_list_branches_carries_pagination_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fleet.gitea_server as server

    class _BranchesClient:
        async def __aenter__(self) -> _BranchesClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def list_branches(self, owner, repo, limit=30):
            return [{"name": "main"}]

    monkeypatch.setattr(server, "_get_client", lambda: _BranchesClient())

    result = await server.gitea_list_branches("gpakoh", "repo", limit=10)

    assert result["ok"] is True
    assert result["result"]["per_page"] == 10
    assert result["result"]["truncated"] is False  # 1 < 10, last page


@pytest.mark.asyncio
async def test_fleet_github_list_pull_requests_carries_pagination_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fleet.github_server as server

    class _PrsClient:
        async def __aenter__(self) -> _PrsClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def list_pull_requests(self, owner, repo, state="open", per_page=30):
            return [dict(GITEA_RAW_ISSUE) for _ in range(per_page)]

    monkeypatch.setattr(server, "_get_client", lambda: _PrsClient())

    result = await server.github_list_pull_requests("gpakoh", "repo")

    assert result["ok"] is True
    assert result["result"]["per_page"] == 30
    assert result["result"]["truncated"] is True


@pytest.mark.asyncio
async def test_fleet_gitea_get_issue_strips_user_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.gitea_server as server

    monkeypatch.setattr(
        server, "_get_client", lambda: _IssueClient(issue=dict(GITEA_RAW_ISSUE))
    )

    result = await server.gitea_get_issue("gpakoh", "repo", 42)

    assert result["ok"] is True
    assert "email" not in str(result)
    assert "is_admin" not in str(result)
    assert "last_login" not in str(result)
    assert result["result"]["user"] == {"login": "gpakoh", "full_name": "Real Name"}


@pytest.mark.asyncio
async def test_fleet_github_list_issues_strips_user_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.github_server as server

    raw = dict(GITEA_RAW_ISSUE)
    raw["user"] = {"login": "gpakoh", "id": 2, "email": "gpakoh@example.com", "node_id": "MDQ6VXNlcjI="}
    monkeypatch.setattr(server, "_get_client", lambda: _IssueClient(issues=[raw]))

    result = await server.github_list_issues("gpakoh", "repo")

    assert result["ok"] is True
    assert "email" not in str(result)
    assert "node_id" not in str(result)
    assert result["result"]["items"][0]["user"] == {"login": "gpakoh"}


@pytest.mark.asyncio
async def test_fleet_github_get_pull_request_strips_user_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.github_server as server

    raw = dict(GITEA_RAW_ISSUE)
    raw["pull_request"] = {"url": "https://api.github.com/pulls/42"}
    raw["draft"] = True
    monkeypatch.setattr(server, "_get_client", lambda: _IssueClient(pr=raw))

    result = await server.github_get_pull_request("gpakoh", "repo", 42)

    assert result["ok"] is True
    assert "email" not in str(result)
    assert "is_admin" not in str(result)
    assert "last_login" not in str(result)
    assert result["result"]["draft"] is True


class _HugeBodyClient(_IssueClient):
    """_IssueClient variant whose get_issue returns a gigantic body."""


@pytest.mark.asyncio
async def test_fleet_gitea_list_issues_truncates_huge_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.gitea_server as server

    raw = dict(GITEA_RAW_ISSUE)
    raw["body"] = "Dependabot dependency update " * 500  # ~15 KB
    monkeypatch.setattr(server, "_get_client", lambda: _IssueClient(issues=[raw]))

    result = await server.gitea_list_issues("gpakoh", "repo")

    assert result["ok"] is True
    item = result["result"]["items"][0]
    assert item["body_truncated"] is True
    assert len(item["body"]) < 6000
    assert "Dependabot dependency update" in item["body"]
    assert "truncated" in item["body"]


@pytest.mark.asyncio
async def test_fleet_github_get_issue_keeps_short_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.github_server as server

    raw = dict(GITEA_RAW_ISSUE)
    raw["body"] = "Short repro"
    monkeypatch.setattr(server, "_get_client", lambda: _IssueClient(issue=raw))

    result = await server.github_get_issue("gpakoh", "repo", 42)

    assert result["ok"] is True
    item = result["result"]
    assert item["body"] == "Short repro"
    assert item.get("body_truncated") is False
