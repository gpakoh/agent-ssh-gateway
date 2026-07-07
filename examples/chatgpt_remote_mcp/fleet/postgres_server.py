"""Postgres read-only MCP adapter — exposes rag_vectordb as read-only tools."""

from __future__ import annotations

import os
import threading

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from postgres_client import PostgresClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .shared import extract_auth_token, get_fleet_env

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
    return f"postgresql://{user}:{password}@{host}:{port}/{db}?sslmode={sslmode}&application_name={appname}"


def _get_client() -> PostgresClient:
    return PostgresClient(_dsn())


mcp = FastMCP("postgres-readonly")


@mcp.tool()
async def postgres_health() -> str:
    """Check Postgres connectivity. Returns DB name, user, version."""
    client = _get_client()
    try:
        info = await client.health()
        return f"ok | db={info['db']} user={info['user']} version={info['version']}"
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
async def postgres_list_schemas() -> str:
    """List non-system schemas in the database."""
    client = _get_client()
    schemas = await client.list_schemas()
    if not schemas:
        return "No user schemas found"
    lines = "\n".join(f"  {s}" for s in schemas)
    return f"Schemas ({len(schemas)}):\n{lines}"


@mcp.tool()
async def postgres_list_tables(
    schema: str = "public",
) -> str:
    """List tables in a schema with type and row estimate.

    schema: schema name (default: public).
    """
    client = _get_client()
    tables = await client.list_tables(schema=schema)
    if not tables:
        return f"No tables found in schema '{schema}'"
    lines = "\n".join(
        f"  {t['table_name']:30s} {t['table_type']:15s} rows={t.get('row_estimate', '?')}"
        for t in tables
    )
    return f"Tables in '{schema}' ({len(tables)}):\n{lines}"


@mcp.tool()
async def postgres_describe_table(
    table_name: str,
    schema: str = "public",
) -> str:
    """Describe columns of a table.

    table_name: name of the table (required).
    schema: schema name (default: public).
    """
    client = _get_client()
    columns = await client.describe_table(schema=schema, table_name=table_name)
    if not columns:
        return f"Table '{schema}.{table_name}' not found or has no columns"
    lines = "\n".join(
        f"  {c['column_name']:30s} {c['data_type']:20s} nullable={c['is_nullable']:5s} default={c.get('column_default', 'NULL')}"
        for c in columns
    )
    return f"Columns of '{schema}.{table_name}' ({len(columns)}):\n{lines}"


@mcp.tool()
async def postgres_select(sql: str) -> str:
    """Execute a read-only SELECT or WITH query with enforced LIMIT 1000.

    sql: SELECT or WITH query (multi-statement not allowed, DDL/DML blocked).
    Returns: JSON array of rows.
    """
    client = _get_client()
    try:
        rows = await client.execute(sql)
    except ValueError as e:
        return f"error: {e}"
    except Exception as e:
        return f"error: query failed: {e}"
    import json

    return json.dumps(rows, default=str, ensure_ascii=False)


@mcp.tool()
async def postgres_vector_status() -> str:
    """Check if pgvector extension is installed and its version."""
    client = _get_client()
    info = await client.vector_status()
    if info["installed"]:
        return f"pgvector is installed (version {info['version']})"
    return "pgvector is NOT installed"


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
