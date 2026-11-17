"""Bounded, credential-free service probes inside the MCP container network.

This is NOT a host port/PID observer. Caller inputs cannot change the host,
port, path, method, request headers or namespace. Docker Compose is the trusted
source for the two static service endpoints.
"""

from __future__ import annotations

import asyncio
import errno
import socket
from datetime import UTC, datetime
from typing import Literal

import httpx
from tool_results import tool_error, tool_success

from examples.mcp_server.mcp_infra.tool_registry import register_tool

Target = Literal["gateway", "mcp"]
_TARGETS: dict[str, tuple[str, int, str]] = {
    "gateway": ("web-ssh-gateway", 8085, "/health"),
    "mcp": ("mcp-server", 8087, "/healthz"),
}
_MIN_TIMEOUT_MS = 200
_MAX_TIMEOUT_MS = 5000
_MAX_BODY_BYTES = 4096


def _invalid(tool: str, field: str, allowed: object) -> dict:
    return tool_error(
        tool=tool,
        code="INVALID_INPUT",
        message="Invalid service probe input",
        details={"field": field, "allowed_values": allowed},
        source="gateway",
    )


def _validate(tool: str, target: str, timeout_ms: int, max_response_bytes: int | None = None) -> dict | None:
    if not isinstance(target, str) or target not in _TARGETS:
        return _invalid(tool, "target", sorted(_TARGETS))
    if type(timeout_ms) is not int or not _MIN_TIMEOUT_MS <= timeout_ms <= _MAX_TIMEOUT_MS:
        return _invalid(tool, "timeout_ms", {"min": _MIN_TIMEOUT_MS, "max": _MAX_TIMEOUT_MS})
    if max_response_bytes is not None and (
        type(max_response_bytes) is not int or not 0 <= max_response_bytes <= _MAX_BODY_BYTES
    ):
        return _invalid(tool, "max_response_bytes", {"min": 0, "max": _MAX_BODY_BYTES})
    return None


def _endpoint(target: str, *, protocol: str) -> dict:
    host, port, path = _TARGETS[target]
    return {
        "target": target,
        "network_namespace": "mcp_container",
        "profile_source": "fixed_gateway_compose_service",
        "host_alias": host,
        "port": port,
        "protocol": protocol,
        "path": path if protocol == "http" else None,
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "host_port_owner_known": False,
    }


def _failure_class(exc: BaseException) -> str:
    """Only fixed categories, never exception strings, URLs or raw socket args."""
    seen: set[int] = set()
    candidate: BaseException | None = exc
    for _ in range(5):
        if candidate is None or id(candidate) in seen:
            break
        seen.add(id(candidate))
        if isinstance(candidate, socket.gaierror):
            return "dns_resolution"
        if isinstance(candidate, ConnectionRefusedError) or (
            isinstance(candidate, OSError) and candidate.errno == errno.ECONNREFUSED
        ):
            return "connection_refused"
        if isinstance(candidate, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
            return "timeout"
        candidate = candidate.__cause__ or candidate.__context__
    return "transport_unavailable"


def _failed(tool: str, endpoint: dict, exc: BaseException) -> dict:
    failure_class = _failure_class(exc)
    return tool_error(
        tool=tool,
        code="TIMEOUT" if failure_class == "timeout" else "REMOTE_UNAVAILABLE",
        message="Bounded service probe could not complete",
        retryable=True,
        details={"failure_class": failure_class, **endpoint},
        source="gateway",
    )


def _http_client(timeout_seconds: float) -> httpx.AsyncClient:
    # trust_env=False prevents ambient proxy/credential inheritance.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        trust_env=False,
        follow_redirects=False,
        headers={"Accept": "application/json"},
    )


async def service_http_probe(
    target: Target = "gateway", timeout_ms: int = 1500, max_response_bytes: int = 1024
) -> dict:
    """Probe one fixed internal GET health path; never return response content."""
    tool = "service_http_probe"
    invalid = _validate(tool, target, timeout_ms, max_response_bytes)
    if invalid is not None:
        return invalid

    endpoint = _endpoint(target, protocol="http")
    host, port, path = _TARGETS[target]
    timeout_seconds = timeout_ms / 1000
    sampled = 0
    truncated = False
    try:
        async with asyncio.timeout(timeout_seconds):
            async with _http_client(timeout_seconds) as client:
                async with client.stream("GET", f"http://{host}:{port}{path}") as response:
                    # Raw chunks prevent decompression bombs; sample at most N+1
                    # bytes even if upstream streams unbounded content.
                    if max_response_bytes:
                        async for chunk in response.aiter_raw(chunk_size=min(max_response_bytes + 1, 1024)):
                            sampled += min(len(chunk), max_response_bytes + 1 - sampled)
                            if sampled > max_response_bytes:
                                truncated = True
                                break
                    status = response.status_code
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        return _failed(tool, endpoint, exc)

    return tool_success(
        tool,
        result={
            **endpoint,
            "method": "GET",
            "status_code": status,
            "healthy": 200 <= status < 300,
            "failure_class": None if 200 <= status < 300 else (
                "redirect_not_followed" if 300 <= status < 400 else "http_non_2xx"
            ),
            "redirect_followed": False,
            "response_body_included": False,
            "response_bytes_sampled": min(sampled, max_response_bytes),
            "response_truncated": truncated,
            "timeout_ms": timeout_ms,
        },
        source="gateway",
        truncated=truncated,
    )


async def service_tcp_probe(target: Target = "gateway", timeout_ms: int = 1500) -> dict:
    """Attempt a bounded TCP connect to an exact internal service port."""
    tool = "service_tcp_probe"
    invalid = _validate(tool, target, timeout_ms)
    if invalid is not None:
        return invalid
    endpoint = _endpoint(target, protocol="tcp")
    host, port, _ = _TARGETS[target]
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout_ms / 1000
        )
        del reader
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        except (TimeoutError, ConnectionError, OSError):
            pass  # No data sent; connection is already closing.
    except (TimeoutError, OSError) as exc:
        return _failed(tool, endpoint, exc)
    return tool_success(tool, result={
        **endpoint,
        "connected": True,
        "application_health": "unknown",
        "timeout_ms": timeout_ms,
        "data_sent": False,
        "response_body_included": False,
    }, source="gateway")


def register_all() -> None:
    register_tool("service_http_probe")(service_http_probe)
    register_tool("service_tcp_probe")(service_tcp_probe)
