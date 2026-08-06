"""Gitea MCP adapter — read-only REST wrapper incl. Actions/CI for ChatGPT."""

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

from .gitea_client import GiteaClient
from .shared import (
    extract_auth_token,
    get_fleet_env,
    minimize_repo_payload,
    normalize_list_response,
    remote_api_error,
    tool_success,
)

INTERNAL_PORT = 8782

HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _get_client() -> GiteaClient:
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        raise RuntimeError("GITEA_TOKEN env var is required")
    return GiteaClient(token)


mcp = FastMCP("gitea-remote")


@mcp.tool()
async def gitea_get_repo(owner: str, repo: str) -> dict[str, Any]:
    """Get repository metadata including description, visibility, language, default branch."""
    try:
        async with _get_client() as client:
            data = await client.get_repo(owner, repo)
    except Exception as exc:
        return remote_api_error("gitea_get_repo", "gitea", exc)
    return tool_success("gitea_get_repo", minimize_repo_payload(data, provider="gitea"), source="gitea")


@mcp.tool()
async def gitea_list_branches(
    owner: str,
    repo: str,
    limit: int = 30,
) -> dict[str, Any]:
    """List branches in a repository. Returns branch names and commit SHAs."""
    try:
        async with _get_client() as client:
            data = normalize_list_response(await client.list_branches(owner, repo, limit=limit))
    except Exception as exc:
        return remote_api_error("gitea_list_branches", "gitea", exc)
    return tool_success("gitea_list_branches", data, source="gitea")


@mcp.tool()
async def gitea_list_commits(
    owner: str,
    repo: str,
    sha: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """List commits in a repository. Optionally filter by branch SHA."""
    try:
        async with _get_client() as client:
            data = normalize_list_response(
                await client.list_commits(owner, repo, sha=sha, limit=limit)
            )
    except Exception as exc:
        return remote_api_error("gitea_list_commits", "gitea", exc)
    return tool_success("gitea_list_commits", data, source="gitea")


@mcp.tool()
async def gitea_get_file(
    owner: str,
    repo: str,
    path: str,
    branch: str | None = None,
) -> dict[str, Any]:
    """Get a file or directory contents from a repository. Returns base64-encoded content or directory listing."""
    try:
        async with _get_client() as client:
            data = await client.get_file(owner, repo, path, branch=branch)
    except Exception as exc:
        return remote_api_error("gitea_get_file", "gitea", exc)
    return tool_success("gitea_get_file", data, source="gitea")


@mcp.tool()
async def gitea_list_issues(
    owner: str,
    repo: str,
    state: str = "open",
    limit: int = 30,
) -> dict[str, Any]:
    """List issues in a repository. State: open, closed, all."""
    try:
        async with _get_client() as client:
            data = normalize_list_response(
                await client.list_issues(owner, repo, state=state, limit=limit)
            )
    except Exception as exc:
        return remote_api_error("gitea_list_issues", "gitea", exc)
    return tool_success("gitea_list_issues", data, source="gitea")


@mcp.tool()
async def gitea_get_issue(
    owner: str,
    repo: str,
    issue_number: int,
) -> dict[str, Any]:
    """Get details of a specific issue by number."""
    try:
        async with _get_client() as client:
            data = await client.get_issue(owner, repo, issue_number)
    except Exception as exc:
        return remote_api_error("gitea_get_issue", "gitea", exc)
    return tool_success("gitea_get_issue", data, source="gitea")


@mcp.tool()
async def gitea_list_pull_requests(
    owner: str,
    repo: str,
    state: str = "open",
    limit: int = 30,
) -> dict[str, Any]:
    """List pull requests in a repository. State: open, closed, all."""
    try:
        async with _get_client() as client:
            data = normalize_list_response(
                await client.list_pull_requests(owner, repo, state=state, limit=limit)
            )
    except Exception as exc:
        return remote_api_error("gitea_list_pull_requests", "gitea", exc)
    return tool_success("gitea_list_pull_requests", data, source="gitea")


@mcp.tool()
async def gitea_get_pull_request(
    owner: str,
    repo: str,
    pull_number: int,
) -> dict[str, Any]:
    """Get details of a specific pull request by number."""
    try:
        async with _get_client() as client:
            data = await client.get_pull_request(owner, repo, pull_number)
    except Exception as exc:
        return remote_api_error("gitea_get_pull_request", "gitea", exc)
    return tool_success("gitea_get_pull_request", data, source="gitea")


# ── Gitea Actions (CI/CD) ────────────────────────────────────────


@mcp.tool()
async def gitea_list_action_runs(
    owner: str,
    repo: str,
    status: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """List Gitea Actions workflow runs. Optionally filter by status (completed, running, waiting)."""
    try:
        async with _get_client() as client:
            data = await client.list_action_runs(owner, repo, status=status, limit=limit)
    except Exception as exc:
        return remote_api_error("gitea_list_action_runs", "gitea", exc)
    return tool_success("gitea_list_action_runs", data, source="gitea")


@mcp.tool()
async def gitea_get_action_run(
    owner: str,
    repo: str,
    run_id: int,
) -> dict[str, Any]:
    """Get details of a specific Gitea Actions workflow run by ID."""
    try:
        async with _get_client() as client:
            data = await client.get_action_run(owner, repo, run_id)
    except Exception as exc:
        return remote_api_error("gitea_get_action_run", "gitea", exc)
    return tool_success("gitea_get_action_run", data, source="gitea")


@mcp.tool()
async def gitea_list_action_run_jobs(
    owner: str,
    repo: str,
    run_id: int,
) -> dict[str, Any]:
    """List jobs and their steps for a specific Gitea Actions workflow run."""
    try:
        async with _get_client() as client:
            data = await client.list_action_run_jobs(owner, repo, run_id)
    except Exception as exc:
        return remote_api_error("gitea_list_action_run_jobs", "gitea", exc)
    return tool_success("gitea_list_action_run_jobs", data, source="gitea")


@mcp.tool()
async def gitea_list_workflows(
    owner: str,
    repo: str,
) -> dict[str, Any]:
    """List Gitea Actions workflow files in a repository."""
    try:
        async with _get_client() as client:
            data = await client.list_workflows(owner, repo)
    except Exception as exc:
        return remote_api_error("gitea_list_workflows", "gitea", exc)
    return tool_success("gitea_list_workflows", data, source="gitea")


# ── Auth proxy ───────────────────────────────────────────────────
def create_auth_proxy(*, upstream_port: int, valid_tokens: set[str]) -> Starlette:
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{upstream_port}",
        timeout=HTTP_TIMEOUT,
    )

    async def proxy(request: Request) -> Response:
        token = extract_auth_token(request, valid_tokens)
        if not token:
            return JSONResponse({"error": "missing or invalid auth"}, 401)

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


# ── Main ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    env = get_fleet_env()

    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = INTERNAL_PORT
    threading.Thread(
        target=mcp.run,
        kwargs={"transport": "streamable-http"},
        daemon=True,
    ).start()

    app = create_auth_proxy(upstream_port=INTERNAL_PORT, valid_tokens={env["token"]})
    uvicorn.run(app, host=env["host"], port=env["port"])
