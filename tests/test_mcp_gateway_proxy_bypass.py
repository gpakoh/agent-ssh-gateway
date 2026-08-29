"""Regressions for the MCP-to-Gateway ambient-proxy trust boundary."""

from __future__ import annotations

import ast
import asyncio
import json
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = ROOT / "examples" / "mcp_server"
if str(MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_SERVER_DIR))

from gateway_client import GatewayClient  # noqa: E402, I001


RequestRecord = dict[str, Any]


@contextmanager
def _recording_server(*, status: int, response: dict[str, Any]) -> Iterator[tuple[int, list[RequestRecord]]]:
    records: list[RequestRecord] = []
    body = json.dumps(response).encode()

    class Handler(BaseHTTPRequestHandler):
        def _respond(self) -> None:
            records.append(
                {
                    "method": self.command,
                    "path": self.path,
                    # Record presence only. Tests never retain credential material.
                    "has_gateway_api_key": "X-API-Key" in self.headers,
                }
            )
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _respond
        do_POST = _respond

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _force_web_gateway_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    original_getaddrinfo = socket.getaddrinfo

    def direct_gateway(host: str | bytes | None, port: Any, *args: Any, **kwargs: Any) -> Any:
        normalized = host.decode() if isinstance(host, bytes) else host
        if normalized == "web-ssh-gateway":
            host = "127.0.0.1"
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", direct_gateway)


def _set_rejecting_proxy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proxy_port: int,
) -> None:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    # Reproduce production: the real Docker service alias is deliberately absent.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,agent-ssh-gateway")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,agent-ssh-gateway")
    _force_web_gateway_dns(monkeypatch)


def test_sync_internal_gateway_health_bypasses_rejecting_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        _recording_server(status=403, response={"error": "proxy rejected"}) as (
            proxy_port,
            proxy_requests,
        ),
        _recording_server(status=200, response={"status": "ok", "ready": True}) as (
            gateway_port,
            gateway_requests,
        ),
    ):
        _set_rejecting_proxy(monkeypatch, proxy_port=proxy_port)
        client = GatewayClient(
            base_url=f"http://web-ssh-gateway:{gateway_port}",
            api_key="test-gateway-key",
        )

        assert client.health() == {"status": "ok", "ready": True}

    assert proxy_requests == []
    assert gateway_requests == [
        {"method": "GET", "path": "/health", "has_gateway_api_key": True}
    ]


def test_async_internal_gateway_post_bypasses_proxy_without_exposing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> tuple[dict[str, Any], list[RequestRecord], list[RequestRecord]]:
        with (
            _recording_server(status=403, response={"error": "proxy rejected"}) as (
                proxy_port,
                proxy_requests,
            ),
            _recording_server(status=200, response={"status": "disconnected"}) as (
                gateway_port,
                gateway_requests,
            ),
        ):
            _set_rejecting_proxy(monkeypatch, proxy_port=proxy_port)
            client = GatewayClient(
                base_url=f"http://web-ssh-gateway:{gateway_port}",
                api_key="test-gateway-key",
            )

            result = await client._post_async(
                "/api/ssh/disconnect",
                {"session_id": "test-session"},
                timeout=2,
            )
        return result, proxy_requests, gateway_requests

    result, proxy_requests, gateway_requests = asyncio.run(exercise())

    assert result == {"status": "disconnected"}
    assert proxy_requests == []
    assert gateway_requests == [
        {
            "method": "POST",
            "path": "/api/ssh/disconnect",
            "has_gateway_api_key": True,
        }
    ]


def test_every_internal_gateway_httpx_entry_point_disables_environment_proxies() -> None:
    source_path = MCP_SERVER_DIR / "gateway_client.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    internal_calls: list[tuple[str, int, bool]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "httpx":
            continue
        if node.func.attr not in {"get", "post", "AsyncClient"}:
            continue
        trust_env = next((keyword.value for keyword in node.keywords if keyword.arg == "trust_env"), None)
        is_disabled = isinstance(trust_env, ast.Constant) and trust_env.value is False
        internal_calls.append((node.func.attr, node.lineno, is_disabled))

    assert len(internal_calls) == 4
    assert all(disabled for _, _, disabled in internal_calls), internal_calls


def test_compose_no_proxy_defense_in_depth_includes_real_gateway_alias() -> None:
    compose = (ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")

    assert compose.count("NO_PROXY=${NO_PROXY:-127.0.0.1,localhost},web-ssh-gateway") == 2
    assert compose.count("no_proxy=${no_proxy:-127.0.0.1,localhost},web-ssh-gateway") == 2


def test_external_gitea_egress_keeps_environment_proxy_behavior() -> None:
    source_path = MCP_SERVER_DIR / "control_plane_git.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    client_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
        and node.func.attr == "Client"
    ]

    assert len(client_calls) == 1
    assert all(keyword.arg != "trust_env" for keyword in client_calls[0].keywords)
