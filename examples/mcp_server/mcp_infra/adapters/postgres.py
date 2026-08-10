"""Postgres adapter: read-only SQL inspection tools.

_get_pg_client is resolved through the server module at call time: tests
monkeypatch server._get_pg_client and expect the patched client here.
"""

import json
from typing import Any

from tool_results import tool_error, tool_success

from examples.mcp_server.mcp_infra.tool_registry import register_tool


def _json_safe(value: Any) -> Any:
    """Round-trip through json.dumps(default=str) to coerce driver-native
    types (datetime, Decimal, UUID, ...) into JSON-safe primitives, while
    keeping the result a real structure (dict/list/etc.) rather than a
    JSON string -- callers still get structured data, not a second
    parsing step."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _get_pg_client():
    from examples.mcp_server import server as _server

    return _server._get_pg_client()


def _postgres_not_configured(tool: str) -> dict[str, Any]:
    return tool_error(
        tool=tool,
        code="DEPENDENCY_MISSING",
        message="Postgres not configured (PG DSN missing)",
        retryable=False,
        source="postgres",
    )


async def postgres_health() -> dict[str, Any]:
    """Check Postgres connectivity. Returns DB name, user, version."""
    client = _get_pg_client()
    if client is None:
        return _postgres_not_configured("postgres_health")
    try:
        info = await client.health()
    except Exception as e:
        return tool_error(
            tool="postgres_health", code="INTERNAL_ERROR", message=str(e), source="postgres"
        )
    return tool_success("postgres_health", result=_json_safe(info), source="postgres")


async def postgres_list_schemas() -> dict[str, Any]:
    """List non-system schemas in the database."""
    client = _get_pg_client()
    if client is None:
        return _postgres_not_configured("postgres_list_schemas")
    try:
        schemas = await client.list_schemas()
    except ValueError as e:
        return tool_error(
            tool="postgres_list_schemas", code="INVALID_INPUT", message=str(e), source="postgres"
        )
    except Exception as e:
        return tool_error(
            tool="postgres_list_schemas",
            code="INTERNAL_ERROR",
            message=f"list schemas failed: {e}",
            source="postgres",
        )
    return tool_success(
        "postgres_list_schemas",
        result={"schemas": schemas, "count": len(schemas)},
        source="postgres",
    )


async def postgres_list_tables(schema: str = "public") -> dict[str, Any]:
    """List tables in a schema with type and row estimate."""
    client = _get_pg_client()
    if client is None:
        return _postgres_not_configured("postgres_list_tables")
    try:
        tables = await client.list_tables(schema=schema)
    except ValueError as e:
        return tool_error(
            tool="postgres_list_tables", code="INVALID_INPUT", message=str(e), source="postgres"
        )
    except Exception as e:
        return tool_error(
            tool="postgres_list_tables",
            code="INTERNAL_ERROR",
            message=f"list tables failed: {e}",
            source="postgres",
        )
    return tool_success(
        "postgres_list_tables",
        result={"schema": schema, "tables": _json_safe(tables), "count": len(tables)},
        source="postgres",
    )


async def postgres_describe_table(table_name: str, schema: str = "public") -> dict[str, Any]:
    """Describe columns of a table."""
    client = _get_pg_client()
    if client is None:
        return _postgres_not_configured("postgres_describe_table")
    try:
        columns = await client.describe_table(schema=schema, table_name=table_name)
    except ValueError as e:
        return tool_error(
            tool="postgres_describe_table", code="INVALID_INPUT", message=str(e), source="postgres"
        )
    except Exception as e:
        return tool_error(
            tool="postgres_describe_table",
            code="INTERNAL_ERROR",
            message=f"describe table failed: {e}",
            source="postgres",
        )
    if not columns:
        return tool_error(
            tool="postgres_describe_table",
            code="FILE_NOT_FOUND",
            message=f"Table '{schema}.{table_name}' not found or has no columns",
            retryable=False,
            source="postgres",
        )
    return tool_success(
        "postgres_describe_table",
        result={
            "schema": schema,
            "table_name": table_name,
            "columns": _json_safe(columns),
            "count": len(columns),
        },
        source="postgres",
    )


async def postgres_select(sql: str) -> dict[str, Any]:
    """Execute a read-only SELECT or WITH query with enforced LIMIT 1000.
    Multi-statement not allowed, DDL/DML blocked."""
    client = _get_pg_client()
    if client is None:
        return _postgres_not_configured("postgres_select")
    try:
        rows = await client.execute(sql)
    except ValueError as e:
        return tool_error(
            tool="postgres_select", code="INVALID_INPUT", message=str(e), source="postgres"
        )
    except Exception as e:
        return tool_error(
            tool="postgres_select",
            code="INTERNAL_ERROR",
            message=f"query failed: {e}",
            source="postgres",
        )
    return tool_success(
        "postgres_select",
        result={"rows": _json_safe(rows), "row_count": len(rows)},
        source="postgres",
    )


async def postgres_vector_status() -> dict[str, Any]:
    """Check if pgvector extension is installed and its version."""
    client = _get_pg_client()
    if client is None:
        return _postgres_not_configured("postgres_vector_status")
    try:
        info = await client.vector_status()
    except ValueError as e:
        return tool_error(
            tool="postgres_vector_status", code="INVALID_INPUT", message=str(e), source="postgres"
        )
    except Exception as e:
        return tool_error(
            tool="postgres_vector_status",
            code="INTERNAL_ERROR",
            message=f"vector status failed: {e}",
            source="postgres",
        )
    return tool_success("postgres_vector_status", result=info, source="postgres")


def register_all() -> None:
    register_tool("postgres_health")(postgres_health)
    register_tool("postgres_list_schemas")(postgres_list_schemas)
    register_tool("postgres_list_tables")(postgres_list_tables)
    register_tool("postgres_describe_table")(postgres_describe_table)
    register_tool("postgres_select")(postgres_select)
    register_tool("postgres_vector_status")(postgres_vector_status)
