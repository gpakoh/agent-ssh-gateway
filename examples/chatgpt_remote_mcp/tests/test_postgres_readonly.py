"""Tests for Postgres read-only MCP adapter."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("PGHOST", "127.0.0.1")
os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "rag_vectordb")
os.environ.setdefault("PGUSER", "mcp_readonly")
os.environ.setdefault("PGPASSWORD", "test123")


class MockRecord(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as err:
            raise AttributeError(name) from err


def _make_conn_mock(rows=None, row=None):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchrow = AsyncMock(return_value=row or None)
    return conn


def _make_pool_mock(conn: MagicMock | None = None) -> MagicMock:
    if conn is None:
        conn = _make_conn_mock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


class TestGuardrails:
    def _make_client(self, pool: MagicMock | None = None):
        from fleet.postgres_client import PostgresClient

        client = PostgresClient("postgresql://u:p@h:5432/db")
        if pool:
            client._pool = pool
        return client

    @pytest.mark.asyncio
    async def test_trailing_semicolon_allowed(self):
        client = self._make_client(pool=_make_pool_mock())
        result = await client.execute("SELECT 1;")
        assert result == []

    @pytest.mark.asyncio
    async def test_semicolon_in_middle_rejected(self):
        client = self._make_client(pool=_make_pool_mock())
        with pytest.raises(ValueError, match="Semicolon only allowed at end"):
            await client.execute("SELECT 1; DROP TABLE users")

    @pytest.mark.asyncio
    async def test_multi_statement_rejected(self):
        client = self._make_client(pool=_make_pool_mock())
        with pytest.raises(ValueError, match="Multi-statement"):
            await client.execute("SELECT 1; SELECT 2;")

    @pytest.mark.asyncio
    async def test_leading_semicolon_rejected(self):
        client = self._make_client(pool=_make_pool_mock())
        with pytest.raises(ValueError, match="Semicolon only allowed at end"):
            await client.execute("; SELECT 1")

    @pytest.mark.asyncio
    async def test_not_select_rejected(self):
        client = self._make_client(pool=_make_pool_mock())
        for sql in [
            "INSERT INTO users VALUES (1)",
            "UPDATE users SET x = 1",
            "DELETE FROM users",
            "COPY users FROM '/tmp/x'",
            "CREATE EXTENSION vector",
            "DROP TABLE users",
            "ALTER TABLE users ADD COLUMN x INT",
            "SET search_path TO public",
            "SET ROLE admin",
        ]:
            with pytest.raises(ValueError, match="Only SELECT|WITH"):
                await client.execute(sql)

    @pytest.mark.asyncio
    async def test_system_schema_rejected(self):
        client = self._make_client(pool=_make_pool_mock())
        for sql in [
            "SELECT * FROM pg_catalog.pg_class",
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM pg_toast.pg_toast_1234",
        ]:
            with pytest.raises(ValueError, match="system schemas"):
                await client.execute(sql)

    @pytest.mark.asyncio
    async def test_pg_sleep_rejected(self):
        client = self._make_client(pool=_make_pool_mock())
        with pytest.raises(ValueError, match="not allowed"):
            await client.execute("SELECT pg_sleep(10)")

    @pytest.mark.asyncio
    async def test_limit_enforced(self):
        conn = _make_conn_mock()
        pool = _make_pool_mock(conn)
        client = self._make_client(pool)
        await client.execute("SELECT * FROM users")
        called_sql = conn.fetch.call_args[0][0]
        assert "LIMIT 1000" in called_sql

    @pytest.mark.asyncio
    async def test_valid_select_passes(self):
        conn = _make_conn_mock()
        pool = _make_pool_mock(conn)
        client = self._make_client(pool)
        for sql in [
            "SELECT 1",
            "SELECT * FROM users LIMIT 10",
            "WITH cte AS (SELECT 1) SELECT * FROM cte",
            "select count(*) from users",
        ]:
            await client.execute(sql)

    @pytest.mark.asyncio
    async def test_wrapping_format(self):
        conn = _make_conn_mock()
        pool = _make_pool_mock(conn)
        client = self._make_client(pool)
        await client.execute("SELECT 1")
        wrapped = conn.fetch.call_args[0][0]
        assert wrapped == "SELECT * FROM (SELECT 1) AS _mcp_subquery LIMIT 1000"

    @pytest.mark.asyncio
    async def test_sql_exceeds_max_length(self):
        client = self._make_client(pool=_make_pool_mock())
        with pytest.raises(ValueError, match="max length"):
            await client.execute("S" * 9000)


class TestPostgresClient:
    @pytest.mark.asyncio
    async def test_health_ok(self):
        row = MockRecord({"ok": True, "db": "rag", "user": "mcp_readonly", "version": "PG 15"})
        conn = _make_conn_mock(row=row)
        pool = _make_pool_mock(conn)
        from fleet.postgres_client import PostgresClient

        client = PostgresClient("postgresql://u:p@h:5432/db")
        client._pool = pool
        result = await client.health()
        assert result["ok"] is True
        assert result["db"] == "rag"

    @pytest.mark.asyncio
    async def test_list_schemas(self):
        rows = [MockRecord({"schema_name": "public"}), MockRecord({"schema_name": "extensions"})]
        conn = _make_conn_mock(rows=rows)
        pool = _make_pool_mock(conn)
        from fleet.postgres_client import PostgresClient

        client = PostgresClient("postgresql://u:p@h:5432/db")
        client._pool = pool
        result = await client.list_schemas()
        assert result == ["public", "extensions"]

    @pytest.mark.asyncio
    async def test_list_tables(self):
        rows = [
            MockRecord({"table_name": "users", "table_type": "BASE TABLE", "row_estimate": 100}),
            MockRecord({"table_name": "docs", "table_type": "BASE TABLE", "row_estimate": 50}),
        ]
        conn = _make_conn_mock(rows=rows)
        pool = _make_pool_mock(conn)
        from fleet.postgres_client import PostgresClient

        client = PostgresClient("postgresql://u:p@h:5432/db")
        client._pool = pool
        result = await client.list_tables()
        assert len(result) == 2
        assert result[0]["table_name"] == "users"

    @pytest.mark.asyncio
    async def test_describe_table(self):
        rows = [
            MockRecord({
                "column_name": "id", "data_type": "integer",
                "is_nullable": "NO", "column_default": "nextval(...)"
            }),
        ]
        conn = _make_conn_mock(rows=rows)
        pool = _make_pool_mock(conn)
        from fleet.postgres_client import PostgresClient

        client = PostgresClient("postgresql://u:p@h:5432/db")
        client._pool = pool
        result = await client.describe_table("public", "users")
        assert result[0]["column_name"] == "id"

    @pytest.mark.asyncio
    async def test_vector_status_installed(self):
        rows = [MockRecord({"extname": "vector", "extversion": "0.6.0"})]
        conn = _make_conn_mock(rows=rows)
        pool = _make_pool_mock(conn)
        from fleet.postgres_client import PostgresClient

        client = PostgresClient("postgresql://u:p@h:5432/db")
        client._pool = pool
        result = await client.vector_status()
        assert result["installed"] is True
        assert result["version"] == "0.6.0"

    @pytest.mark.asyncio
    async def test_vector_status_not_installed(self):
        conn = _make_conn_mock(rows=[])
        pool = _make_pool_mock(conn)
        from fleet.postgres_client import PostgresClient

        client = PostgresClient("postgresql://u:p@h:5432/db")
        client._pool = pool
        result = await client.vector_status()
        assert result["installed"] is False



