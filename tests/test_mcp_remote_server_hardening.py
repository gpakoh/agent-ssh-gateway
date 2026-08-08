"""Regression tests for examples/mcp_client_remote/server.py hardening.

Findings from a live security audit:
- MCP_SCOPE_ENFORCEMENT defaulted to "off" in source -- a missing/forgotten
  env var silently disabled all tool-level scope enforcement for the
  authenticated MCP proxy. Must default to "enforce" (fail closed).
- proxy_request()'s PROXY log line included the full upstream `url`, which
  in `token` rollback mode carries the live `?mcp_token=<secret>` query
  string verbatim into the application log at INFO level.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest


class TestScopeEnforcementDefaultsClosed:
    def test_default_is_enforce_not_off(self, monkeypatch):
        monkeypatch.delenv("MCP_SCOPE_ENFORCEMENT", raising=False)
        with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": "t", "MCP_AUTH_MODE": "token"}):
            import importlib

            import examples.mcp_client_remote.server as srv

            importlib.reload(srv)
            assert srv.MCP_SCOPE_ENFORCEMENT == "enforce"

    def test_explicit_off_still_works(self, monkeypatch):
        """The fix must not remove the ability to explicitly opt out."""
        with patch.dict(
            os.environ,
            {"MCP_PUBLIC_TOKEN": "t", "MCP_AUTH_MODE": "token", "MCP_SCOPE_ENFORCEMENT": "off"},
        ):
            import importlib

            import examples.mcp_client_remote.server as srv

            importlib.reload(srv)
            assert srv.MCP_SCOPE_ENFORCEMENT == "off"


class _FakeUpstreamResponse:
    status_code = 200
    headers: dict[str, str] = {}

    async def aiter_bytes(self):
        yield b'{"ok": true}'

    async def aread(self):
        return b""


class TestProxyLogDoesNotLeakMcpToken:
    @pytest.mark.asyncio
    async def test_mcp_token_query_param_not_logged(self, caplog, monkeypatch):
        with patch.dict(
            os.environ,
            {
                "MCP_PUBLIC_TOKEN": "SUPERSECRETTOKEN",
                "MCP_AUTH_MODE": "token",
                "MCP_SCOPE_ENFORCEMENT": "off",
            },
        ):
            import importlib

            import examples.mcp_client_remote.server as srv

            importlib.reload(srv)

            monkeypatch.setattr(
                srv.httpx.AsyncClient,
                "request",
                AsyncMock(return_value=_FakeUpstreamResponse()),
            )

            from starlette.testclient import TestClient

            with caplog.at_level("INFO", logger="mcp_client_remote"):
                with TestClient(srv.create_proxy_app()) as client:
                    resp = client.get(
                        "/mcp",
                        params={"mcp_token": "SUPERSECRETTOKEN"},
                    )
            assert resp.status_code == 200
            log_text = "\n".join(r.getMessage() for r in caplog.records)
            assert "SUPERSECRETTOKEN" not in log_text
            assert "PROXY GET /mcp" in log_text
