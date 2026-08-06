"""Postgres read-only MCP adapter — exposes example_vectordb as read-only tools."""

from __future__ import annotations

import asyncio
import os
import sys
import threading

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from postgres_client import PostgresClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .shared import (
    extract_auth_token,
    get_fleet_env,
    json_safe,
    resolve_docker_host,
    tool_error,
    tool_success,
)

INTERNAL_PORT = 8784
HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _dsn() -> str:
    host = os.environ["PGHOST"]
    port = os.environ.get("PGPORT", "5432")
    db = os.environ["PGDATABASE"]
    user = os.environ["PGUSER"]
    password = os.environ["PGPASSWORD"]
    sslmode = os.environ.get("PGSSLMODE", "disable")
    appname = os.environ.get("PGAPPNAME", "mcp_readonly")
    # A static /etc/hosts entry for PGHOST drifts the moment mcp-postgres is
    # ever recreated — resolve live instead of trusting it stayed in sync.
    resolved_host = resolve_docker_host(host)
    if resolved_host != host:
        print(f"  resolved {host} -> {resolved_host} via docker inspect", file=sys.stderr)
    return f"postgresql://{user}:{password}@{resolved_host}:{port}/{db}?sslmode={sslmode}&application_name={appname}"


_client: PostgresClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> PostgresClient:
    """Return the single shared PostgresClient for this process.

    Regression: every tool call used to construct a brand new PostgresClient
    (via `PostgresClient(_dsn())`), and PostgresClient._ensure_pool() always
    creates a fresh asyncpg pool (min_size=1) since a new instance's _pool is
    always None — nothing ever closed it. Confirmed live: 5 tool calls left 6
    idle "mcp_readonly" connections in pg_stat_activity, growing without
    bound on every subsequent call, in a service that runs indefinitely.
    Left unfixed, sustained use exhausts Postgres's max_connections for
    every client of that instance, not just this adapter.
    """
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = PostgresClient(_dsn())
    return _client


mcp = FastMCP("postgres-readonly")


@mcp.tool()
async def postgres_health() -> dict:
    """Check Postgres connectivity. Returns DB name, user, version."""
    client = await _get_client()
    try:
        info = await client.health()
    except Exception as exc:
        return tool_error(
            "postgres_health",
            "POSTGRES_ERROR",
            str(exc),
            source="postgres",
            retryable=True,
        )
    return tool_success("postgres_health", json_safe(info), source="postgres")


@mcp.tool()
async def postgres_list_schemas() -> dict:
    """List non-system schemas in the database."""
    client = await _get_client()
    try:
        schemas = await client.list_schemas()
    except Exception as exc:
        return tool_error(
            "postgres_list_schemas",
            "POSTGRES_ERROR",
            str(exc),
            source="postgres",
            retryable=True,
        )
    return tool_success(
        "postgres_list_schemas",
        {"schemas": schemas, "count": len(schemas)},
        source="postgres",
    )


@mcp.tool()
async def postgres_list_tables(
    schema: str = "public",
) -> dict:
    """List tables in a schema with type and row estimate."""
    client = await _get_client()
    try:
        tables = await client.list_tables(schema=schema)
    except ValueError as exc:
        return tool_error(
            "postgres_list_tables",
            "INVALID_INPUT",
            str(exc),
            source="postgres",
        )
    except Exception as exc:
        return tool_error(
            "postgres_list_tables",
            "POSTGRES_ERROR",
            str(exc),
            source="postgres",
            retryable=True,
        )
    return tool_success(
        "postgres_list_tables",
        {"schema": schema, "tables": json_safe(tables), "count": len(tables)},
        source="postgres",
    )


@mcp.tool()
async def postgres_describe_table(
    table_name: str,
    schema: str = "public",
) -> dict:
    """Describe columns of a table."""
    client = await _get_client()
    try:
        columns = await client.describe_table(schema=schema, table_name=table_name)
    except ValueError as exc:
        return tool_error(
            "postgres_describe_table",
            "INVALID_INPUT",
            str(exc),
            source="postgres",
        )
    except Exception as exc:
        return tool_error(
            "postgres_describe_table",
            "POSTGRES_ERROR",
            str(exc),
            source="postgres",
            retryable=True,
        )
    if not columns:
        return tool_error(
            "postgres_describe_table",
            "TABLE_NOT_FOUND",
            f"Table '{schema}.{table_name}' not found or has no columns",
            source="postgres",
        )
    return tool_success(
        "postgres_describe_table",
        {
            "schema": schema,
            "table_name": table_name,
            "columns": json_safe(columns),
            "count": len(columns),
        },
        source="postgres",
    )


@mcp.tool()
async def postgres_select(sql: str) -> dict:
    """Execute a read-only SELECT or WITH query with enforced LIMIT 1000."""
    client = await _get_client()
    try:
        rows = await client.execute(sql)
    except ValueError as exc:
        return tool_error(
            "postgres_select",
            "INVALID_INPUT",
            str(exc),
            source="postgres",
        )
    except Exception as exc:
        return tool_error(
            "postgres_select",
            "POSTGRES_ERROR",
            f"query failed: {exc}",
            source="postgres",
            retryable=True,
        )
    return tool_success(
        "postgres_select",
        {"rows": json_safe(rows), "row_count": len(rows)},
        source="postgres",
    )


@mcp.tool()
async def postgres_vector_status() -> dict:
    """Check if pgvector extension is installed and its version."""
    client = await _get_client()
    try:
        info = await client.vector_status()
    except Exception as exc:
        return tool_error(
            "postgres_vector_status",
            "POSTGRES_ERROR",
            str(exc),
            source="postgres",
            retryable=True,
        )
    return tool_success("postgres_vector_status", json_safe(info), source="postgres")


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
        resp_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in ("transfer-encoding", "content-length", "date", "server")
        }
        resp_headers.setdefault("content-type", "application/json")
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
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
