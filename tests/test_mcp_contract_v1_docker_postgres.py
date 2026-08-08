"""Contract v1 regression tests for docker_*/postgres_* MCP tool wrappers.

Before this fix, docker_ps/docker_images/docker_inspect/docker_logs/
docker_stats and all six postgres_* tools returned bare strings (formatted
tables, hand-built text, or json.dumps() output) instead of the canonical
{"ok": ..., "result": ..., "error": ..., "meta": ...} envelope every other
tool uses -- and even where a payload happened to be JSON, it arrived as a
string requiring a second parse, not structured data. Both are fixed at
the source (DockerClient/PostgresClient callers + the wrapper functions
below), not by a generic response-shape shim.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_MCP_SERVER_DIR = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server")
if _MCP_SERVER_DIR not in sys.path:
    sys.path.insert(0, _MCP_SERVER_DIR)

from examples.mcp_server import server as mcp_server_mod  # noqa: E402


def _assert_envelope(result: dict, *, ok: bool = True) -> None:
    assert result["ok"] is ok
    assert "result" in result
    assert "error" in result
    assert "meta" in result
    assert result["meta"]["contract_version"] == "1"


class TestDockerToolsContractV1:
    @pytest.mark.asyncio
    async def test_docker_ps_returns_structured_result(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.ps = AsyncMock(return_value=[{"Names": "web", "Status": "Up"}])
        monkeypatch.setattr(mcp_server_mod, "DockerClient", lambda: fake_client)

        result = await mcp_server_mod.docker_ps()
        _assert_envelope(result)
        assert result["result"]["containers"] == [{"Names": "web", "Status": "Up"}]
        assert result["result"]["count"] == 1
        assert result["meta"]["source"] == "docker"

    @pytest.mark.asyncio
    async def test_docker_ps_error_is_contract_v1(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.ps = AsyncMock(side_effect=RuntimeError("docker exited 1: no such host"))
        monkeypatch.setattr(mcp_server_mod, "DockerClient", lambda: fake_client)

        result = await mcp_server_mod.docker_ps()
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "DOCKER_COMMAND_FAILED"

    @pytest.mark.asyncio
    async def test_docker_images_returns_structured_result(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.images = AsyncMock(return_value=[{"Repository": "nginx", "Tag": "alpine"}])
        monkeypatch.setattr(mcp_server_mod, "DockerClient", lambda: fake_client)

        result = await mcp_server_mod.docker_images()
        _assert_envelope(result)
        assert result["result"]["images"] == [{"Repository": "nginx", "Tag": "alpine"}]

    @pytest.mark.asyncio
    async def test_docker_inspect_returns_structured_result(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.inspect = AsyncMock(return_value=[{"Id": "abc123"}])
        monkeypatch.setattr(mcp_server_mod, "DockerClient", lambda: fake_client)

        result = await mcp_server_mod.docker_inspect("web")
        _assert_envelope(result)
        assert result["result"] == [{"Id": "abc123"}]

    @pytest.mark.asyncio
    async def test_docker_logs_returns_structured_lines(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.logs = AsyncMock(return_value={"lines": ["a", "b"], "count": 2})
        monkeypatch.setattr(mcp_server_mod, "DockerClient", lambda: fake_client)

        result = await mcp_server_mod.docker_logs("web")
        _assert_envelope(result)
        assert result["result"] == {"lines": ["a", "b"], "count": 2}

    @pytest.mark.asyncio
    async def test_docker_stats_returns_structured_result(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.stats = AsyncMock(return_value=[{"Name": "web", "CPUPerc": "0.5%"}])
        monkeypatch.setattr(mcp_server_mod, "DockerClient", lambda: fake_client)

        result = await mcp_server_mod.docker_stats()
        _assert_envelope(result)
        assert result["result"]["stats"] == [{"Name": "web", "CPUPerc": "0.5%"}]


class TestPostgresToolsContractV1:
    def _patch_client(self, monkeypatch, client) -> None:
        monkeypatch.setattr(mcp_server_mod, "_get_pg_client", lambda: client)

    @pytest.mark.asyncio
    async def test_postgres_health_returns_structured_result(self, monkeypatch):
        client = MagicMock()
        client.health = AsyncMock(return_value={"ok": 1, "db": "mydb", "user": "ro", "version": "PostgreSQL 16"})
        self._patch_client(monkeypatch, client)

        result = await mcp_server_mod.postgres_health()
        _assert_envelope(result)
        assert result["result"]["db"] == "mydb"
        assert result["meta"]["source"] == "postgres"

    @pytest.mark.asyncio
    async def test_postgres_not_configured_is_contract_v1_error(self, monkeypatch):
        self._patch_client(monkeypatch, None)

        result = await mcp_server_mod.postgres_health()
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "DEPENDENCY_MISSING"

    @pytest.mark.asyncio
    async def test_postgres_list_schemas_returns_structured_list(self, monkeypatch):
        client = MagicMock()
        client.list_schemas = AsyncMock(return_value=["public", "app"])
        self._patch_client(monkeypatch, client)

        result = await mcp_server_mod.postgres_list_schemas()
        _assert_envelope(result)
        assert result["result"] == {"schemas": ["public", "app"], "count": 2}

    @pytest.mark.asyncio
    async def test_postgres_list_tables_returns_structured_list(self, monkeypatch):
        client = MagicMock()
        client.list_tables = AsyncMock(
            return_value=[{"table_name": "users", "table_type": "BASE TABLE", "row_estimate": 10}]
        )
        self._patch_client(monkeypatch, client)

        result = await mcp_server_mod.postgres_list_tables(schema="public")
        _assert_envelope(result)
        assert result["result"]["schema"] == "public"
        assert result["result"]["tables"][0]["table_name"] == "users"

    @pytest.mark.asyncio
    async def test_postgres_describe_table_not_found_is_contract_v1_error(self, monkeypatch):
        client = MagicMock()
        client.describe_table = AsyncMock(return_value=[])
        self._patch_client(monkeypatch, client)

        result = await mcp_server_mod.postgres_describe_table("missing_table")
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "FILE_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_postgres_select_returns_structured_rows_not_json_string(self, monkeypatch):
        client = MagicMock()
        client.execute = AsyncMock(return_value=[{"database_name": "mydb"}])
        self._patch_client(monkeypatch, client)

        result = await mcp_server_mod.postgres_select("SELECT current_database() AS database_name")
        _assert_envelope(result)
        # Regression: this used to be json.dumps(rows) -- a string the
        # caller had to parse a second time. Now it's a real structure.
        assert isinstance(result["result"]["rows"], list)
        assert result["result"]["rows"] == [{"database_name": "mydb"}]
        assert result["result"]["row_count"] == 1

    @pytest.mark.asyncio
    async def test_postgres_select_invalid_sql_is_contract_v1_error(self, monkeypatch):
        client = MagicMock()
        client.execute = AsyncMock(side_effect=ValueError("Only SELECT and WITH queries are allowed"))
        self._patch_client(monkeypatch, client)

        result = await mcp_server_mod.postgres_select("DELETE FROM users")
        _assert_envelope(result, ok=False)
        assert result["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_postgres_select_coerces_non_json_native_types(self, monkeypatch):
        """asyncpg rows can contain datetime/Decimal/UUID -- the final
        envelope goes through json.dumps() with no default=str, so the
        structured result must already be JSON-safe primitives."""
        import datetime
        import decimal

        client = MagicMock()
        client.execute = AsyncMock(
            return_value=[{"created_at": datetime.datetime(2026, 1, 1), "amount": decimal.Decimal("9.99")}]
        )
        self._patch_client(monkeypatch, client)

        result = await mcp_server_mod.postgres_select("SELECT created_at, amount FROM t")
        _assert_envelope(result)
        row = result["result"]["rows"][0]
        assert row["created_at"] == "2026-01-01 00:00:00"
        assert row["amount"] == "9.99"
        import json

        json.dumps(result)  # must not raise

    @pytest.mark.asyncio
    async def test_postgres_vector_status_returns_structured_result(self, monkeypatch):
        client = MagicMock()
        client.vector_status = AsyncMock(return_value={"installed": True, "version": "0.7.0"})
        self._patch_client(monkeypatch, client)

        result = await mcp_server_mod.postgres_vector_status()
        _assert_envelope(result)
        assert result["result"] == {"installed": True, "version": "0.7.0"}
