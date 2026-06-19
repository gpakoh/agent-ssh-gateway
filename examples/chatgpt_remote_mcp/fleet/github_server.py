"""GitHub MCP adapter — read-only REST wrapper for ChatGPT remote access."""

from __future__ import annotations

import os
import threading
from typing import Any

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .github_client import GitHubClient
from .shared import get_fleet_env

# ── Config ────────────────────────────────────────────────────────────
INTERNAL_PORT = 8781  # FastMCP streamable-http (no auth, localhost only)

HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _get_client() -> GitHubClient:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN env var is required")
    return GitHubClient(token)


# ── FastMCP with tools ────────────────────────────────────────────────
mcp = FastMCP("github-remote")


@mcp.tool()
async def github_get_repo(owner: str, repo: str) -> dict[str, Any]:
    """Get repository metadata including description, stars, forks, language, topics."""
    async with _get_client() as client:
        return await client.get_repo(owner, repo)


@mcp.tool()
async def github_list_branches(
    owner: str, repo: str, per_page: int = 30,
) -> list[dict[str, Any]]:
    """List branches in a repository. Returns branch names and commit SHAs."""
    async with _get_client() as client:
        return await client.list_branches(owner, repo, per_page=per_page)


@mcp.tool()
async def github_list_commits(
    owner: str, repo: str,
    sha: str | None = None, per_page: int = 30,
) -> list[dict[str, Any]]:
    """List commits in a repository. Optionally filter by branch SHA."""
    async with _get_client() as client:
        return await client.list_commits(
            owner, repo, sha=sha, per_page=per_page,
        )


@mcp.tool()
async def github_get_file(
    owner: str, repo: str, path: str,
    branch: str | None = None,
) -> dict[str, Any]:
    """Get a file or directory contents from a repository. Returns base64-encoded content or directory listing."""
    async with _get_client() as client:
        return await client.get_file(owner, repo, path, branch=branch)


@mcp.tool()
async def github_list_issues(
    owner: str, repo: str,
    state: str = "open", per_page: int = 30,
) -> list[dict[str, Any]]:
    """List issues in a repository. State: open, closed, all."""
    async with _get_client() as client:
        return await client.list_issues(
            owner, repo, state=state, per_page=per_page,
        )


@mcp.tool()
async def github_get_issue(
    owner: str, repo: str, issue_number: int,
) -> dict[str, Any]:
    """Get details of a specific issue by number."""
    async with _get_client() as client:
        return await client.get_issue(owner, repo, issue_number)


@mcp.tool()
async def github_list_pull_requests(
    owner: str, repo: str,
    state: str = "open", per_page: int = 30,
) -> list[dict[str, Any]]:
    """List pull requests in a repository. State: open, closed, all."""
    async with _get_client() as client:
        return await client.list_pull_requests(
            owner, repo, state=state, per_page=per_page,
        )


@mcp.tool()
async def github_get_pull_request(
    owner: str, repo: str, pull_number: int,
) -> dict[str, Any]:
    """Get details of a specific pull request by number."""
    async with _get_client() as client:
        return await client.get_pull_request(owner, repo, pull_number)


# ── Auth proxy ────────────────────────────────────────────────────
def create_auth_proxy(
    *, upstream_port: int, valid_tokens: set[str]
) -> Starlette:
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{upstream_port}",
        timeout=HTTP_TIMEOUT,
    )

    async def proxy(request: Request) -> Response:
        token = request.query_params.get("mcp_token")
        if not token:
            return JSONResponse({"error": "missing mcp_token"}, 401)
        if token not in valid_tokens:
            return JSONResponse({"error": "invalid mcp_token"}, 403)

        body = await request.body()
        headers = dict(request.headers)
        headers.pop("host", None)
        resp = await client.post(
            "/mcp",
            content=body,
            headers=headers,
            params={k: v for k, v in request.query_params.items() if k != "mcp_token"},
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        )

    return Starlette(routes=[Route("/mcp", endpoint=proxy, methods=["POST"])])


# ── Main ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    env = get_fleet_env()

    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = INTERNAL_PORT
    threading.Thread(
        target=mcp.run,
        kwargs={"transport": "streamable-http"},
        daemon=True,
    ).start()

    app = create_auth_proxy(
        upstream_port=INTERNAL_PORT, valid_tokens={env["token"]}
    )
    uvicorn.run(app, host=env["host"], port=env["port"])
