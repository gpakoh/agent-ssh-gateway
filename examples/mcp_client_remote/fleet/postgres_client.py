"""Read-only asyncpg wrapper for Postgres MCP adapter.

SQL guardrails:
- Multi-statement ban (0-1 trailing semicolon allowed)
- Only SELECT/WITH queries
- DDL/DML/DCL keyword block
- System schema reference block for user queries
- Dangerous function block
- SET/role switching block
- Max row limit enforced via subquery wrapping
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg

QUERY_TIMEOUT = 30
MAX_RESULT_ROWS = 1000
MAX_SQL_LENGTH = 8192

SYSTEM_SCHEMA_RE = re.compile(
    r"\b(pg_catalog|information_schema|pg_toast)\b",
    re.IGNORECASE,
)

DDL_BLOCKLIST = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE"
    r"|COPY|CALL|VACUUM|ANALYZE|GRANT|REVOKE|LISTEN|NOTIFY"
    r"|EXECUTE|IMPORT|EXPLAIN\s+ANALYZE"
    r"|pg_sleep|pg_terminate_backend|pg_cancel_backend"
    r"|lo_import|lo_export)\b",
    re.IGNORECASE,
)


class PostgresClient:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=1,
                max_size=5,
                command_timeout=QUERY_TIMEOUT,
                server_settings={
                    "default_transaction_read_only": "on",
                    "statement_timeout": f"{QUERY_TIMEOUT * 1000}",
                },
            )
        return self._pool

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        sql_stripped = sql.strip()
        if len(sql_stripped) > MAX_SQL_LENGTH:
            raise ValueError(f"SQL exceeds max length ({len(sql_stripped)} > {MAX_SQL_LENGTH})")

        semicolon_count = sql_stripped.count(";")
        if semicolon_count > 1:
            raise ValueError("Multi-statement queries are not allowed")
        if semicolon_count == 1 and not sql_stripped.endswith(";"):
            raise ValueError("Semicolon only allowed at end of query")

        clean_sql = sql_stripped.rstrip(";").strip()

        if not re.match(r"^\s*(SELECT|WITH)\b", clean_sql, re.IGNORECASE):
            raise ValueError("Only SELECT and WITH queries are allowed")

        if SYSTEM_SCHEMA_RE.search(clean_sql):
            raise ValueError(
                "Queries referencing system schemas (pg_catalog, information_schema) are not allowed"
            )

        if DDL_BLOCKLIST.search(clean_sql):
            raise ValueError("DDL, DML, and dangerous statements are not allowed")

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            wrapped = f"SELECT * FROM ({clean_sql}) AS _mcp_subquery LIMIT {MAX_RESULT_ROWS}"
            rows = await conn.fetch(wrapped, *params)

        result = [dict(row) for row in rows]
        for row in result:
            for k, v in row.items():
                if isinstance(v, memoryview | bytes):
                    row[k] = str(v)
        return result

    async def execute_internal(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            if args:
                rows = await conn.fetch(sql, *args)
            else:
                rows = await conn.fetch(sql)
        return [dict(row) for row in rows]

    async def health(self) -> dict[str, Any]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 AS ok, current_database() AS db, current_user AS user, version() AS version"
            )
            if row is None:
                return {"ok": False, "error": "no row returned"}
            return dict(row)

    async def list_schemas(self) -> list[str]:
        rows = await self.execute_internal(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
            "ORDER BY schema_name"
        )
        return [r["schema_name"] for r in rows]

    async def list_tables(self, schema: str = "public") -> list[dict[str, Any]]:
        rows = await self.execute_internal(
            "SELECT table_name, table_type, "
            "(SELECT reltuples::bigint FROM pg_class WHERE oid = (quote_ident($1) || '.' || quote_ident(table_name))::regclass) AS row_estimate "
            "FROM information_schema.tables "
            "WHERE table_schema = $1 "
            "ORDER BY table_name "
            "LIMIT 100",
            schema,
        )
        return [dict(r) for r in rows]

    async def describe_table(self, schema: str, table_name: str) -> list[dict[str, Any]]:
        rows = await self.execute_internal(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = $2 "
            "ORDER BY ordinal_position",
            schema,
            table_name,
        )
        return [dict(r) for r in rows]

    async def vector_status(self) -> dict[str, Any]:
        rows = await self.execute_internal(
            "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'"
        )
        if rows:
            return {"installed": True, "version": rows[0]["extversion"]}
        return {"installed": False, "version": None}
