"""Docker MCP adapter — read-only subprocess wrapper for ChatGPT remote access."""

from __future__ import annotations

import threading

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .docker_client import DockerClient
from .shared import extract_auth_token, get_fleet_env

INTERNAL_PORT = 8793
HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _get_client() -> DockerClient:
    return DockerClient()


mcp = FastMCP("docker-remote")


@mcp.tool()
async def docker_ps(
    all: bool = False,
    format: str | None = None,
    limit: int = 50,
) -> list[dict] | str:
    """List running containers. Use all=True to include stopped containers.

    Returns structured rows (docker's native --format json) by default.
    format: Go template string for docker ps --format overrides this and
    returns the raw string instead, for callers that need a specific shape.
    limit: max containers to return (default 50, set higher for full list).
    """
    client = _get_client()
    return await client.ps(all=all, format=format or None, limit=limit)


@mcp.tool()
async def docker_images(
    format: str | None = None,
    limit: int = 50,
) -> list[dict] | str:
    """List Docker images on the host.

    Returns structured rows by default. format: Go template string for
    docker images --format overrides this and returns the raw string.
    limit: max images to return (default 50, set higher for full list).
    """
    client = _get_client()
    return await client.images(format=format or None, limit=limit)


@mcp.tool()
async def docker_inspect(name: str) -> list[dict] | dict:
    """Inspect a container by name or ID. Returns structured metadata (first 500 entries)."""
    client = _get_client()
    return await client.inspect(name, max_lines=500)


@mcp.tool()
async def docker_logs(container: str, tail: int = 200) -> dict:
    """Fetch logs from a running container as {"lines": [...], "count": N}.

    container: container name or ID.
    tail: number of recent lines (1-1000, default 200).
    """
    client = _get_client()
    return await client.logs(container, tail=tail)


@mcp.tool()
async def docker_stats(
    format: str | None = None,
    limit: int = 50,
) -> list[dict] | str:
    """Show live resource usage statistics for all running containers (CPU, memory, network, block I/O).

    Returns structured rows by default. format: Go template string overrides
    this and returns the raw string.
    limit: max containers to return (default 50, set higher for full list).
    """
    client = _get_client()
    return await client.stats(format=format or None, limit=limit)


@mcp.tool()
async def docker_compose_ps(
    project_dir: str | None = None,
    limit: int = 50,
) -> list[dict] | str:
    """List containers in a Docker Compose project.

    Returns structured rows by default.
    project_dir: path to directory containing compose file (e.g. /media/1TB/Python/web_ssh/web-ssh-gateway/docker).
    limit: max services to return (default 50).
    """
    client = _get_client()
    return await client.compose_ps(project_dir=project_dir, limit=limit)


@mcp.tool()
async def docker_compose_services(
    project_dir: str | None = None,
) -> dict:
    """List service names defined in a Docker Compose project (uses compose config --services).

    project_dir: path to directory containing compose file.
    """
    client = _get_client()
    return await client.compose_services(project_dir=project_dir)


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
