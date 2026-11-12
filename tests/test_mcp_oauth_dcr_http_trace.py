"""Bounded, credential-free HTTPX transport trace for OAuth DCR (#801).

The real proxy already emits DCR_TRACE around upstream_send/headers. These
tests prove the same random ID can differentiate connect, upload and waiting
for headers without ever recording request/response metadata or secrets.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
    monkeypatch.setenv("MCP_SCOPE_ENFORCEMENT", "off")
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://public.test")
    import examples.mcp_client_remote.server as remote

    importlib.reload(remote)
    return remote


class _TracedFakeClient:
    def __init__(self, *, fail: bool = False, cancelled: bool = False):
        self.fail = fail
        self.cancelled = cancelled
        self.last_request: httpx.Request | None = None

    def build_request(self, method, url, content, headers, timeout):
        request = httpx.Request(method, url, content=content, headers=headers)
        request.extensions["timeout"] = timeout.as_dict()
        self.last_request = request
        return request

    async def send(self, request, *, stream):
        assert stream is True
        callback = request.extensions["trace"]
        # httpcore's info can contain raw host, request, response headers and
        # exception details; none may escape through our fixed-shape trace.
        for event in (
            "connection.connect_tcp.started",
            "connection.connect_tcp.complete",
            "http11.send_request_headers.complete",
            "http11.send_request_body.complete",
            "http11.receive_response_headers.started",
        ):
            await callback(event, {"request": request, "secret": "SHOULD_NOT_LOG"})
        if self.cancelled:
            raise asyncio.CancelledError()
        if self.fail:
            await callback(
                "http11.receive_response_headers.failed",
                {"exception": httpx.ReadTimeout("SHOULD_NOT_LOG", request=request)},
            )
            raise httpx.ReadTimeout("SHOULD_NOT_LOG", request=request)
        await callback(
            "http11.receive_response_headers.complete",
            {"return_value": (b"HTTP/1.1", 201, b"OK", [(b"Token", b"SHOULD_NOT_LOG")])},
        )
        return httpx.Response(201, request=request)


def _phases(stderr: str, trace_id: str) -> list[str]:
    lines = stderr.splitlines()
    matches = [
        re.fullmatch(
            rf"DCR_HTTP_TRACE id={trace_id} phase=([a-z_]+) elapsed_ms=[0-9]{{1,7}}",
            line,
        )
        for line in lines
    ]
    return [match.group(1) for match in matches if match is not None]


@pytest.mark.asyncio
async def test_dcr_httpcore_trace_reuses_proxy_id_and_never_leaks_transport_info(
    srv, monkeypatch, capsys
):
    client = _TracedFakeClient()
    monkeypatch.setattr(srv, "_make_upstream_client", lambda _timeout: client)
    trace_id = "abcdef123456"
    result = await srv._proxy_upstream(
        SimpleNamespace(method="POST"),
        "http://internal.test/register?token=PRIVATE_TOKEN",
        b"PRIVATE_BODY",
        {"Authorization": "Bearer PRIVATE_AUTH"},
        target_path="/register",
        dcr_trace_id=trace_id,
        dcr_started=time.monotonic(),
    )
    assert result is not None
    assert result[1].status_code == 201
    assert _phases(capsys.readouterr().err, trace_id) == [
        "tcp_start",
        "tcp_done",
        "req_headers_done",
        "req_body_done",
        "resp_headers_wait",
        "resp_headers_done",
    ]
    assert client.last_request is not None
    # The DCR proxy read budget must stay below the caller's 20s stage.
    assert client.last_request.extensions["timeout"]["read"] == (
        srv._DCR_UPSTREAM_WALL_CLOCK_SECONDS
    )
    assert 0 < srv._DCR_UPSTREAM_WALL_CLOCK_SECONDS < 20


@pytest.mark.asyncio
async def test_dcr_headers_stall_reports_last_boundary_and_sanitized_error(
    srv, monkeypatch, capsys
):
    client = _TracedFakeClient(fail=True)
    monkeypatch.setattr(srv, "_make_upstream_client", lambda _timeout: client)
    result = await srv._proxy_upstream(
        SimpleNamespace(method="POST"),
        "http://internal.test/register?token=PRIVATE_TOKEN",
        b"PRIVATE_BODY",
        {"Authorization": "Bearer PRIVATE_AUTH"},
        target_path="/register",
        dcr_trace_id="012345abcdef",
        dcr_started=time.monotonic(),
    )
    assert result is None
    stderr = capsys.readouterr().err
    assert _phases(stderr, "012345abcdef")[-3:] == [
        "resp_headers_wait",
        "resp_headers_error",
        "transport_error",
    ]
    assert "resp_headers_done" not in stderr
    for secret in ("PRIVATE_TOKEN", "PRIVATE_BODY", "PRIVATE_AUTH", "SHOULD_NOT_LOG"):
        assert secret not in stderr


@pytest.mark.asyncio
async def test_dcr_cancel_is_visible_but_not_swallowed(srv, monkeypatch, capsys):
    monkeypatch.setattr(
        srv,
        "_make_upstream_client",
        lambda _timeout: _TracedFakeClient(cancelled=True),
    )
    with pytest.raises(asyncio.CancelledError):
        await srv._proxy_upstream(
            SimpleNamespace(method="POST"),
            "http://internal.test/register",
            b"",
            {},
            target_path="/register",
            dcr_trace_id="001122aabbcc",
            dcr_started=time.monotonic(),
        )
    assert _phases(capsys.readouterr().err, "001122aabbcc")[-1] == "cancelled"


@pytest.mark.asyncio
async def test_non_dcr_requests_do_not_attach_trace_or_emit_logs(srv, monkeypatch, capsys):
    class PlainFakeClient:
        def build_request(self, method, url, content, headers, timeout):
            req = httpx.Request(method, url, content=content, headers=headers)
            req.extensions["timeout"] = timeout.as_dict()
            return req

        async def send(self, request, *, stream):
            assert "trace" not in request.extensions
            return httpx.Response(200, request=request)

    monkeypatch.setattr(srv, "_make_upstream_client", lambda _timeout: PlainFakeClient())
    result = await srv._proxy_upstream(
        SimpleNamespace(method="POST"),
        "http://internal.test/mcp",
        b"x",
        {},
        target_path="/mcp",
    )
    assert result is not None
    assert "DCR_HTTP_TRACE" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_real_local_httpcore_trace_marks_tcp_body_and_response_headers(srv, capsys):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 201 Created\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\nOK"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    assert listener.sockets
    port = listener.sockets[0].getsockname()[1]
    client = httpx.AsyncClient(trust_env=False)
    srv._UPSTREAM_CLIENT = client
    try:
        upstream = await srv._proxy_upstream(
            SimpleNamespace(method="POST"),
            f"http://127.0.0.1:{port}/register",
            b"payload",
            {},
            target_path="/register",
            dcr_trace_id="aabbcc001122",
            dcr_started=time.monotonic(),
        )
        assert upstream is not None
        _, response = upstream
        assert response.status_code == 201
        assert await response.aread() == b"OK"
        await response.aclose()
    finally:
        await client.aclose()
        srv._UPSTREAM_CLIENT = None
        listener.close()
        await listener.wait_closed()

    phases = _phases(capsys.readouterr().err, "aabbcc001122")
    assert "tcp_start" in phases and "tcp_done" in phases
    assert "req_body_done" in phases
    assert "resp_headers_wait" in phases and "resp_headers_done" in phases
    assert phases.index("resp_headers_wait") < phases.index("resp_headers_done")


@pytest.mark.asyncio
async def test_actual_proxy_forwards_one_trace_id_to_the_httpcore_hook(
    srv, monkeypatch, capsys
):
    client = _TracedFakeClient()
    monkeypatch.setattr(srv, "_make_upstream_client", lambda _timeout: client)

    async def receive():
        return {"type": "http.request", "body": b"PRIVATE_BODY", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/register",
            "raw_path": b"/register",
            "query_string": b"code=PRIVATE_TOKEN",
            "headers": [(b"authorization", b"Bearer PRIVATE_AUTH")],
            "server": ("internal.test", 80),
        },
        receive,
    )
    response = await srv.proxy_request(request)
    assert response.status_code == 201
    output = capsys.readouterr().err
    proxy_lines = [
        line for line in output.splitlines() if line.startswith("DCR_TRACE ")
    ]
    ids = re.findall(r"^DCR_TRACE id=([0-9a-f]{12}) ", output, flags=re.MULTILINE)
    assert len(ids) == 4
    assert len(set(ids)) == 1
    trace_id = ids[0]
    assert len(proxy_lines) == 4
    assert _phases(output, trace_id)[-1] == "resp_headers_done"
    assert "upstream_headers" in output
    for secret in ("PRIVATE_TOKEN", "PRIVATE_BODY", "PRIVATE_AUTH", "SHOULD_NOT_LOG"):
        assert secret not in output


def test_ci_error_filter_is_anchored_and_cannot_echo_raw_oauth_secrets():
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    filters = re.findall(r"\| grep -E '([^']*DCR_HTTP_TRACE[^']*)'", workflow)
    assert len(filters) == 2
    for raw in filters:
        pattern = re.compile(raw)
        assert pattern.fullmatch(
            "DCR_HTTP_TRACE id=012345abcdef phase=resp_headers_wait elapsed_ms=123"
        )
        assert pattern.fullmatch(
            "DCR_TRACE id=012345abcdef phase=upstream_send elapsed_ms=123"
        )
        assert not pattern.fullmatch(
            "DCR_HTTP_TRACE id=012345abcdef phase=bad_secret elapsed_ms=123"
        )
        assert not pattern.fullmatch(
            "DCR_HTTP_TRACE id=012345abcdef phase=resp_headers_wait "
            "elapsed_ms=123 PRIVATE_TOKEN"
        )
