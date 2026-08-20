from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from scripts.mcp_streamable_http_route_probe import (
    build_streamable_http_app,
    find_free_port,
    run_ephemeral_server,
    stop_ephemeral_server,
)

SAFE_ENV = {
    "MCP_GATEWAY_TOOL_MODE": "mcp_client",
    "MCP_CLIENT_SAFE_MODE": "true",
    "MCP_ACCESS_PROFILE": "mcp_client_safe",
    "GATEWAY_API_KEY": "test-only-api-key",
    "GATEWAY_SESSION_ID": "seed-session",
}


def _structured(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None) or []
    assert content, "tool result contained neither structuredContent nor content"
    return json.loads(content[0].text)


@pytest.mark.asyncio
async def test_two_real_streamable_http_clients_keep_gateway_session_state_isolated():
    """TEST-11: prove isolation through two real stateful MCP transports.

    This is intentionally above the unit seam in test_mcp_session_client_affinity:
    a real uvicorn server accepts two Streamable HTTP ClientSession connections,
    FastMCP creates two distinct ServerSession objects, and a test-only tool
    resolves/mutates the same get_gateway_client() path used by production tools.
    No test probe is registered in production code or exposed outside this process.
    """
    with patch.dict(os.environ, SAFE_ENV, clear=False):
        app = build_streamable_http_app()

        import examples.mcp_server.server as srv

        @srv.mcp.tool(name="_test_session_affinity_probe")
        def _test_session_affinity_probe(set_session_id: str | None = None) -> dict[str, Any]:
            scoped = srv.get_gateway_client()
            if set_session_id is not None:
                scoped.session_id = set_session_id
            return {
                "client_identity": id(scoped),
                "session_id": scoped.session_id,
            }

        host = "127.0.0.1"
        port = find_free_port()
        server, thread = run_ephemeral_server(app, host, port)
        url = f"http://{host}:{port}/mcp"
        try:
            async with (
                httpx.AsyncClient(timeout=10.0) as http_a,
                httpx.AsyncClient(timeout=10.0) as http_b,
            ):
                async with streamable_http_client(url, http_client=http_a) as (
                    read_a,
                    write_a,
                    get_sid_a,
                ):
                    async with ClientSession(read_a, write_a) as session_a:
                        await session_a.initialize()
                        async with streamable_http_client(url, http_client=http_b) as (
                            read_b,
                            write_b,
                            get_sid_b,
                        ):
                            async with ClientSession(read_b, write_b) as session_b:
                                await session_b.initialize()

                                sid_a = get_sid_a()
                                sid_b = get_sid_b()
                                assert sid_a
                                assert sid_b
                                assert sid_a != sid_b

                                a0 = _structured(
                                    await session_a.call_tool("_test_session_affinity_probe", {})
                                )
                                b0 = _structured(
                                    await session_b.call_tool("_test_session_affinity_probe", {})
                                )
                                assert a0["client_identity"] != b0["client_identity"]
                                assert a0["session_id"] == "seed-session"
                                assert b0["session_id"] == "seed-session"

                                a1 = _structured(
                                    await session_a.call_tool(
                                        "_test_session_affinity_probe",
                                        {"set_session_id": "transport-a-session"},
                                    )
                                )
                                b1 = _structured(
                                    await session_b.call_tool("_test_session_affinity_probe", {})
                                )
                                assert a1["client_identity"] == a0["client_identity"]
                                assert a1["session_id"] == "transport-a-session"
                                assert b1["client_identity"] == b0["client_identity"]
                                assert b1["session_id"] == "seed-session"

                                b2 = _structured(
                                    await session_b.call_tool(
                                        "_test_session_affinity_probe",
                                        {"set_session_id": "transport-b-session"},
                                    )
                                )
                                a2 = _structured(
                                    await session_a.call_tool("_test_session_affinity_probe", {})
                                )
                                assert b2["client_identity"] == b0["client_identity"]
                                assert b2["session_id"] == "transport-b-session"
                                assert a2["client_identity"] == a0["client_identity"]
                                assert a2["session_id"] == "transport-a-session"
        finally:
            stop_ephemeral_server(server, thread)
