"""Tests for Postgres read-only MCP adapter.

Regression context: this file used to live under
examples/mcp_client_remote/tests/, which pyproject.toml's
testpaths = ["tests"] never collects — every test in it (all SQL
guardrails: multi-statement ban, DDL blocklist, system-schema block, row
limits) has never actually run in CI. It also imports `from fleet...`,
which only resolves when pytest happens to be invoked from inside
examples/mcp_client_remote/ — from the repo root (how CI and every other
test in this suite runs) it fails with ModuleNotFoundError before a single
assertion executes. Moved here and given the same sys.path bootstrap every
other fleet test in this directory already uses.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_MCP_CLIENT_REMOTE = Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"
sys.path.insert(0, str(_MCP_CLIENT_REMOTE))
# postgres_server.py does `from postgres_client import ...` (bare, not
# relative) — mirrors run_postgres_server.py's own sys.path setup for the
# real service, since fleet/ itself must be importable as a top-level dir.
sys.path.insert(0, str(_MCP_CLIENT_REMOTE / "fleet"))

os.environ.setdefault("PGHOST", "127.0.0.1")
os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "example_vectordb")
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
            MockRecord(
                {
                    "column_name": "id",
                    "data_type": "integer",
                    "is_nullable": "NO",
                    "column_default": "nextval(...)",
                }
            ),
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


class TestGetClientIsASingleton:
    """Regression: every postgres_* tool call used to build a brand new
    PostgresClient via `PostgresClient(_dsn())`, and PostgresClient's
    _ensure_pool() always creates a fresh asyncpg pool (min_size=1) because
    a new instance's _pool is always None — nothing ever closed the old
    one. Confirmed live against the real mcp-postgres container: 5 tool
    calls left 6 idle "mcp_readonly" connections in pg_stat_activity,
    growing without bound on every subsequent call, in a service that runs
    indefinitely — eventually exhausts Postgres's max_connections for
    every client of that instance, not just this adapter.
    """

    @pytest.mark.asyncio
    async def test_repeated_calls_reuse_the_same_client(self, monkeypatch):
        import fleet.postgres_server as pg_server

        monkeypatch.setattr(pg_server, "_client", None)
        created = []

        class _FakeClient:
            def __init__(self, dsn):
                created.append(dsn)

        monkeypatch.setattr(pg_server, "PostgresClient", _FakeClient)

        first = await pg_server._get_client()
        second = await pg_server._get_client()
        third = await pg_server._get_client()

        assert first is second is third
        assert len(created) == 1

    @pytest.mark.asyncio
    async def test_concurrent_first_calls_still_create_only_one_client(self, monkeypatch):
        """The lazy-init race: two tool calls landing before the first
        PostgresClient() finishes constructing must not each create their
        own pool.
        """
        import asyncio

        import fleet.postgres_server as pg_server

        monkeypatch.setattr(pg_server, "_client", None)
        created = []

        class _FakeClient:
            def __init__(self, dsn):
                created.append(dsn)

        monkeypatch.setattr(pg_server, "PostgresClient", _FakeClient)

        results = await asyncio.gather(
            pg_server._get_client(),
            pg_server._get_client(),
            pg_server._get_client(),
        )

        assert results[0] is results[1] is results[2]
        assert len(created) == 1


class TestDsnResolvesDockerHostLive:
    """Regression: PGHOST relied on a static /etc/hosts entry that drifts
    the moment mcp-postgres is ever recreated — confirmed live, the real
    running service had a stale entry and every tool call was failing with
    ConnectionRefusedError. _dsn() must resolve the host the same way
    examples/mcp_server/server.py already does for its own Postgres usage.
    """

    def test_dsn_uses_resolved_host_not_raw_pghost(self, monkeypatch):
        import fleet.postgres_server as pg_server

        monkeypatch.setenv("PGHOST", "mcp-postgres")
        monkeypatch.setattr(pg_server, "resolve_docker_host", lambda host, **kw: "172.19.0.99")
        dsn = pg_server._dsn()
        assert "172.19.0.99" in dsn
        assert "mcp-postgres" not in dsn

    def test_dsn_falls_back_to_raw_host_when_resolution_is_a_noop(self, monkeypatch):
        import fleet.postgres_server as pg_server

        monkeypatch.setattr(pg_server, "resolve_docker_host", lambda host, **kw: host)
        dsn = pg_server._dsn()
        assert os.environ["PGHOST"] in dsn


class TestResolveDockerHost:
    """fleet/shared.py's resolve_docker_host — the same live-resolution
    helper examples/mcp_server/server.py already had, now shared so the
    Postgres fleet adapter doesn't depend on a static /etc/hosts entry
    staying in sync with whatever IP mcp-postgres currently has.
    """

    def test_returns_resolved_ip_on_success(self, monkeypatch):
        import subprocess

        from fleet.shared import resolve_docker_host

        def _fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="172.19.0.28\n", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert resolve_docker_host("mcp-postgres") == "172.19.0.28"

    def test_falls_back_to_hostname_on_failure(self, monkeypatch):
        import subprocess

        from fleet.shared import resolve_docker_host

        def _fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no such container")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert resolve_docker_host("mcp-postgres") == "mcp-postgres"

    def test_falls_back_to_hostname_on_exception(self, monkeypatch):
        import subprocess

        from fleet.shared import resolve_docker_host

        def _raise(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="docker", timeout=5)

        monkeypatch.setattr(subprocess, "run", _raise)
        assert resolve_docker_host("mcp-postgres") == "mcp-postgres"
