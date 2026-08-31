"""Regression tests for MCP OAuth reverse-proxy streaming.

Streaming contract for examples/mcp_client_remote/server.py:

- Only GET /mcp (and public GET "/" mapped to /mcp) is a real streaming
  hop: `client.send(req, stream=True)` so the first SSE chunk reaches the
  downstream client before upstream EOF. The read timeout is unbounded
  (read=None) while connect/write/pool stay finite.
- POST/DELETE keep the bounded `client.request(...)` semantics: a single
  finite timeout, body buffered, status/body/headers preserved.
- Upstream resources (httpx.Response + AsyncClient) close exactly with the
  downstream stream lifetime: normal EOF, downstream cancellation, upstream
  read error, 401 path, and pre-header RequestError (+502). The generator's
  finally is the single ownership seam; the private `request._receive` hook
  is never used.
- Secret hygiene: the Authorization header, bearer previews, tokens, and raw
  OAuth 401 bodies never reach the log.

All tests are deterministic: the upstream hop is injected through the
`_make_upstream_client` seam (no free ports, no real sockets) and the app is
driven as a raw ASGI app to observe chunk-level streaming and cancellation.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import httpx
import pytest

SERVER_PATH = Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote" / "server.py"


def _reload_srv(monkeypatch, **env):
    import examples.mcp_client_remote.server as srv

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    importlib.reload(srv)
    return srv


def _oauth_headers() -> list[tuple[bytes, bytes]]:
    return [(b"authorization", b"Bearer test-bearer")]


class _FakeStream:
    """Controllable upstream byte stream.

    Yields `chunks`; optionally holds at chunk >= 1 until `hold_eof` is set;
    can raise `read_error` after chunk index 1; sets all `eof_events` when the
    stream is exhausted. Tracks aclose().
    """

    def __init__(self, chunks, hold_eof=None, eof_events=None, read_error=None):
        self.chunks = list(chunks)
        self.hold_eof = hold_eof
        self.eof_events = list(eof_events or [])
        self.read_error = read_error
        self.aclosed = False

    async def __aiter__(self):
        for i, chunk in enumerate(self.chunks):
            if i >= 1 and self.hold_eof is not None:
                await self.hold_eof.wait()
            if self.read_error is not None and i >= 1:
                raise self.read_error
            yield chunk
        for ev in self.eof_events:
            ev.set()

    async def aclose(self):
        self.aclosed = True


class _FakeResponse:
    def __init__(self, status_code, headers, content=b"", stream=None):
        self.status_code = status_code
        self.headers = dict(headers)
        self._content = content
        self._stream = stream
        self.aclosed = False

    async def aiter_bytes(self):
        if self._stream is not None:
            async for chunk in self._stream:
                yield chunk
        else:
            yield self._content

    async def aread(self):
        if self._stream is not None:
            out = b""
            async for chunk in self._stream:
                out += chunk
            return out
        return self._content

    async def aclose(self):
        self.aclosed = True
        if self._stream is not None:
            await self._stream.aclose()


class _FakeClient:
    """Records the timeout + call shape; mirrors httpx stream/buffered hops."""

    def __init__(self, timeout, raiser=None):
        self.timeout = timeout
        self._raiser = raiser
        self.aclosed = False
        self.last_url = None
        self.send_stream_flag: bool | None = None
        self.used_request = False
        self.req_body = None
        self.responses: list[_FakeResponse] = []

    def build_request(self, method, url, content=None, headers=None, timeout=None):
        self.last_url = url
        self.build_timeout = timeout
        return {"method": method, "url": url, "content": content, "headers": headers}

    async def send(self, req, stream=False):
        self.send_stream_flag = stream
        if self._raiser is not None:
            raise self._raiser
        self.last_response = self.responses.pop(0)
        return self.last_response

    async def request(self, method, url, content=None, headers=None, timeout=None):
        self.used_request = True
        self.last_timeout = timeout
        self.last_url = url
        self.req_body = content
        if self._raiser is not None:
            raise self._raiser
        resp = self.responses.pop(0)
        # Faithful httpx buffered semantics: .request() consumes the whole
        # body before returning, so the first chunk cannot cross the proxy
        # until the upstream stream reaches EOF.
        await resp.aread()
        self.last_response = resp
        return self.last_response

    async def aclose(self):
        self.aclosed = True


class _Recorder:
    def __init__(self):
        self.clients: list[_FakeClient] = []
        self.seen_timeouts: list[float | httpx.Timeout] = []

    def make_factory(self, client_builder):
        def factory(timeout):
            client = client_builder()
            client.timeout = timeout
            self.clients.append(client)
            self.seen_timeouts.append(timeout)
            return client

        return factory


class _ASGIDriver:
    """Bare ASGI driver so tests observe raw chunk messages / cancellation.

    spec_version >= 2.4 keeps StreamingResponse off the collapsing-task-group
    + listen_for_disconnect path, mirroring a modern uvicorn ASGI server.
    """

    def __init__(self, app, method="GET", path="/mcp", headers=None, body=b"", query=b""):
        self.app = app
        self.method = method
        self.sent: list[dict] = []
        self._body = body
        self._headers = headers or []
        self._body_state = "pending"
        self._disconnect_gate = asyncio.Event()
        self.body_buf = b""
        self.status: int | None = None
        self.start_headers: list[tuple[bytes, bytes]] = []
        self.done = asyncio.Event()
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query,
            "root_path": "",
            "headers": self._headers,
            "server": ("test", 80),
            "client": ("test", 12345),
        }

    async def receive(self):
        if self._body_state == "pending":
            self._body_state = "sent"
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self._disconnect_gate.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        self.sent.append(message)
        mtype = message.get("type")
        if mtype == "http.response.start":
            self.status = message["status"]
            self.start_headers = message["headers"]
        elif mtype == "http.response.body":
            self.body_buf += message.get("body", b"")
            if not message.get("more_body", False):
                self.done.set()

    async def run(self):
        await self.app(self.scope, self.receive, self.send)


async def _wait(event, timeout=5.0):
    await asyncio.wait_for(event.wait(), timeout)


def _body_chunks(driver: _ASGIDriver) -> list[bytes]:
    return [m.get("body", b"") for m in driver.sent if m.get("type") == "http.response.body"]


def _header_value(driver: _ASGIDriver, want: str) -> str | None:
    for name, value in driver.start_headers:
        if name.decode("latin-1").lower() == want.lower():
            return value.decode("latin-1")
    return None


@pytest.fixture
def stream_srv(monkeypatch):
    return _reload_srv(
        monkeypatch,
        MCP_AUTH_MODE="oauth",
        MCP_SCOPE_ENFORCEMENT="off",
        MCP_PUBLIC_URL="http://public.test",
    )


async def _drive(app, method, path, headers=None, body=b"", query=b""):
    driver = _ASGIDriver(app, method=method, path=path, headers=headers, body=body, query=query)
    task = asyncio.create_task(driver.run())
    await task
    return driver


class TestFirstChunkBeforeEOF:
    @pytest.mark.asyncio
    async def test_get_mcp_streams_chunk_before_upstream_eof(self, stream_srv, monkeypatch):
        hold_eof = asyncio.Event()
        eof_reached = asyncio.Event()
        stream = _FakeStream([b"data: one\n\n", b"data: two\n\n"], hold_eof=hold_eof, eof_events=[eof_reached])
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {}, stream=stream)]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = _ASGIDriver(app, headers=_oauth_headers())
        task = asyncio.create_task(driver.run())

        # Wait until the first downstream body chunk arrives...
        async def _first_body():
            while not [c for c in _body_chunks(driver) if c]:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_first_body(), 5.0)

        assert driver.status == 200
        # ...and prove it was delivered BEFORE the upstream meaning EOF.
        assert not eof_reached.is_set()
        assert not hold_eof.is_set()
        assert _body_chunks(driver)[0] == b"data: one\n\n"
        assert [c for c in _body_chunks(driver) if c] == [b"data: one\n\n"]

        hold_eof.set()
        await _wait(driver.done)
        assert eof_reached.is_set()
        assert [c for c in _body_chunks(driver) if c] == [b"data: one\n\n", b"data: two\n\n"]

        await task
        client = recorder.clients[0]
        assert not client.aclosed  # shared client survives EOF
        assert stream.aclosed
        assert client.send_stream_flag is True

    @pytest.mark.asyncio
    async def test_root_maps_to_mcp_and_streams(self, stream_srv, monkeypatch):
        stream = _FakeStream([b"data: hi\n\n"])  # immediate EOF
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {}, stream=stream)]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = await _drive(app, "GET", "/", headers=_oauth_headers())

        assert driver.status == 200
        assert recorder.clients[0].last_url.endswith("/mcp")
        assert recorder.clients[0].send_stream_flag is True
        assert driver.body_buf == b"data: hi\n\n"


class TestStreamingTimeout:
    @pytest.mark.asyncio
    async def test_streaming_read_timeout_is_none(self, stream_srv, monkeypatch):
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {}, content=b"ok")]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = await _drive(app, "GET", "/mcp", headers=_oauth_headers())

        assert driver.status == 200
        timeout = recorder.seen_timeouts[0]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read is None

    @pytest.mark.asyncio
    async def test_streaming_connect_write_pool_finite(self, stream_srv, monkeypatch):
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {}, content=b"ok")]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        await _drive(app, "GET", "/mcp", headers=_oauth_headers())

        timeout = recorder.seen_timeouts[0]
        assert isinstance(timeout, httpx.Timeout)
        for phase in ("connect", "write", "pool"):
            value = getattr(timeout, phase)
            assert isinstance(value, float) and value > 0, f"{phase} must be finite"

    @pytest.mark.asyncio
    async def test_long_idle_upstream_no_proxy_read_timeout(self, stream_srv, monkeypatch):
        """An upstream that idles longer than a small default must survive."""
        hold_eof = asyncio.Event()
        eof_reached = asyncio.Event()
        stream = _FakeStream([b"ping\n\n", b"pong\n\n"], hold_eof=hold_eof, eof_events=[eof_reached])
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {}, stream=stream)]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = _ASGIDriver(app, headers=_oauth_headers())
        task = asyncio.create_task(driver.run())

        async def _first_body():
            while not [c for c in _body_chunks(driver) if c]:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_first_body(), 5.0)
        await asyncio.sleep(0.3)  # idle well past any plausibly-small read timeout
        assert not task.done()
        assert eof_reached.is_set() is False

        hold_eof.set()
        await _wait(driver.done)
        assert driver.body_buf == b"ping\n\npong\n\n"
        assert eof_reached.is_set()
        await task


class TestResourceOwnership:
    @pytest.mark.asyncio
    async def test_normal_eof_closes_response_not_shared_client(self, stream_srv, monkeypatch):
        stream = _FakeStream([b"a\n\n", b"b\n\n"])
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {}, stream=stream)]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        await _drive(app, "GET", "/mcp", headers=_oauth_headers())

        client = recorder.clients[0]
        resp = client.last_response
        assert resp.aclosed
        assert not client.aclosed  # shared client survives a single request
        assert stream.aclosed

    @pytest.mark.asyncio
    async def test_downstream_cancel_closes_response_not_shared_client(self, stream_srv, monkeypatch):
        hold_eof = asyncio.Event()
        stream = _FakeStream([b"part1\n\n", b"part2\n\n"], hold_eof=hold_eof)
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {}, stream=stream)]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = _ASGIDriver(app, headers=_oauth_headers())
        task = asyncio.create_task(driver.run())

        async def _first_body():
            while not [c for c in _body_chunks(driver) if c]:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_first_body(), 5.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        client = recorder.clients[0]
        resp = client.last_response
        assert resp.aclosed
        assert not client.aclosed

    @pytest.mark.asyncio
    async def test_upstream_read_error_closes_response_not_shared_client(self, stream_srv, monkeypatch):
        stream = _FakeStream(
            [b"ok\n\n", b"boom\n\n"],
            read_error=httpx.ReadError("upstream died mid-body"),
        )
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {}, stream=stream)]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = _ASGIDriver(app, headers=_oauth_headers())
        task = asyncio.create_task(driver.run())
        try:
            await task
        except BaseException:  # upstream error surfaces through the app
            pass

        client = recorder.clients[0]
        resp = client.last_response
        assert resp.aclosed
        assert not client.aclosed  # shared client survives an upstream read error
        assert stream.aclosed

    @pytest.mark.asyncio
    async def test_preheader_request_error_returns_502_and_preserves_client(self, stream_srv, monkeypatch):
        recorder = _Recorder()

        def build():
            return _FakeClient(None, raiser=httpx.ConnectError("connection refused"))

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = await _drive(app, "GET", "/mcp", headers=_oauth_headers())

        assert driver.status == 502
        assert b"Upstream unreachable" in driver.body_buf
        assert not recorder.clients[0].aclosed  # shared client survives a connect error


class TestBoundedPaths:
    @pytest.mark.asyncio
    async def test_post_streams_through_shared_client(self, stream_srv, monkeypatch):
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {"Content-Type": "application/json"}, content=b'{"ok": true}')]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        body = b'{"jsonrpc":"2.0","method":"tools/list","id":1}'
        driver = await _drive(
            app,
            "POST",
            "/mcp",
            headers=_oauth_headers() + [(b"content-type", b"application/json")],
            body=body,
        )

        client = recorder.clients[0]
        # MCP streamable HTTP is SSE even for POST tool calls: the hop is a
        # stream=True send through the shared client, not a buffered request.
        assert client.used_request is False
        assert client.send_stream_flag is True
        assert client.last_url.endswith("/mcp")
        assert driver.status == 200
        assert driver.body_buf == b'{"ok": true}'
        # every hop keeps the unbounded-read SSE timeout (AUTH-2)
        stream_t = recorder.seen_timeouts[0]
        assert isinstance(stream_t, httpx.Timeout)
        assert stream_t.read is None

    @pytest.mark.asyncio
    async def test_delete_session_termination_stays_bounded(self, stream_srv, monkeypatch):
        """DELETE /mcp terminates a session and must keep a FINITE read timeout
        (not an unbounded read=None) per the timeout matrix (BLOCKING GATE 3)."""
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(204, {}, content=b"")]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = await _drive(app, "DELETE", "/mcp", headers=_oauth_headers())

        client = recorder.clients[0]
        assert client.used_request is False
        assert client.send_stream_flag is True
        assert client.last_url.endswith("/mcp")
        assert driver.status == 204
        stream_t = recorder.seen_timeouts[0]
        assert isinstance(stream_t, httpx.Timeout)
        # bounded: finite read, NOT None
        assert stream_t.read is not None
        assert isinstance(stream_t.read, float) and stream_t.read > 0


class TestHttpSemantics:
    @pytest.mark.asyncio
    async def test_401_body_status_header_preserved_response_closed(self, stream_srv, monkeypatch):
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [
                _FakeResponse(
                    401,
                    {"WWW-Authenticate": 'Bearer realm="mcp"', "Content-Type": "application/json"},
                    content=b'{"error":"invalid_token","error_description":"expired"}',
                )
            ]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = await _drive(app, "GET", "/mcp", headers=_oauth_headers())

        assert driver.status == 401
        assert _header_value(driver, "WWW-Authenticate") == 'Bearer realm="mcp"'
        assert b'"error":"invalid_token"' in driver.body_buf
        client = recorder.clients[0]
        resp = client.last_response
        assert resp.aclosed
        assert not client.aclosed  # shared client survives a 401 pass-through

    @pytest.mark.asyncio
    async def test_mcp_session_id_header_preserved(self, stream_srv, monkeypatch):
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {"Mcp-Session-Id": "session-xyz"}, content=b"ok")]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = await _drive(app, "GET", "/mcp", headers=_oauth_headers())

        assert _header_value(driver, "Mcp-Session-Id") == "session-xyz"

    @pytest.mark.asyncio
    async def test_404_passthrough_semantics(self, stream_srv, monkeypatch):
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(404, {}, content=b"not found")]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        driver = await _drive(app, "GET", "/mcp", headers=_oauth_headers())

        assert driver.status == 404
        assert driver.body_buf == b"not found"


class TestSecretHygiene:
    @pytest.mark.asyncio
    async def test_no_raw_auth_or_bearer_in_logs(self, stream_srv, monkeypatch, caplog):
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(200, {}, content=b"ok")]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()
        headers = [(b"authorization", b"Bearer SUPERSECRET-BEARER-TOKEN")]
        with caplog.at_level("INFO", logger="mcp_client_remote"):
            await _drive(app, "GET", "/mcp", headers=headers)

        log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "SUPERSECRET-BEARER-TOKEN" not in log_text
        assert "Bearer" not in log_text
        assert "Authorization" not in log_text

    @pytest.mark.asyncio
    async def test_no_raw_401_body_in_logs(self, stream_srv, monkeypatch, caplog):
        """The 401 body may embed tokens/credentials - never log it raw."""
        secret = b'{"error":"invalid_token","debug":"accesstoken=AKIA-SUPERSECRET"}'
        recorder = _Recorder()

        def build():
            client = _FakeClient(None)
            client.responses = [_FakeResponse(401, {}, content=secret)]
            return client

        monkeypatch.setattr(stream_srv, "_make_upstream_client", recorder.make_factory(build))
        app = stream_srv.create_proxy_app()

        with caplog.at_level("WARNING", logger="mcp_client_remote"):
            await _drive(app, "GET", "/mcp", headers=_oauth_headers())

        log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "AKIA-SUPERSECRET" not in log_text
        assert "accesstoken=" not in log_text


def test_no_private_receive_hook_in_proxy():
    """The proxy must never touch the private httpx `request._receive` hook."""
    source = SERVER_PATH.read_text()
    proxy_zone = source[source.index("async def proxy_request"):]
    assert "._receive" not in proxy_zone
    assert "_receive" not in proxy_zone