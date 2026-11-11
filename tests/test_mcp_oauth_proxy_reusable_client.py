"""TEST-09: reusable upstream AsyncClient lifecycle contract.

The corrective replaced per-request ``httpx.AsyncClient`` construction
(confirmed hot path behind MCP proxy stalling -- see DIAGNOSTIC_REPORT.md)
with ONE process-shared ``httpx.AsyncClient`` created at lifespan startup
and reused for every upstream hop.

Contract under test:
  * ``_make_upstream_client`` is a sync getter that returns the shared
    process-level client (created once at startup);
  * the shared client is created exactly once and reused across many
    proxy requests (it is never rebuilt per request);
  * the shared client is never closed by a single request -- no matter
    the branch (streaming EOF, downstream cancel, upstream read error,
    401 pass-through, bounded hop, pre-header connect error) only the
    upstream Response is released;
  * the shared client is closed exactly once at lifespan shutdown;
  * per-request timeout override still applies on every hop even though
    the shared client carries a finite default; the per-hop timeout is
    method+path aware (see ``_mcp_proxy_timeout``): GET/POST /mcp -> read=None
    (SSE), DELETE /mcp and non-MCP routes -> finite read (BLOCKING GATE);
  * explicit timeout matrix is verified deterministically (gates 1-4).

We drive the real ASGI app through its actual routes, but point the
shared client at a counting fake so no real sockets / ports are opened.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

SERVER_PATH = Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote" / "server.py"


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
    monkeypatch.setenv("MCP_SCOPE_ENFORCEMENT", "off")
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://public.test")
    monkeypatch.setenv("MCP_INTERNAL_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_INTERNAL_PORT", "8789")
    monkeypatch.delenv("MCP_PUBLIC_TOKEN", raising=False)
    import examples.mcp_client_remote.server as srv
    importlib.reload(srv)
    return srv


def _oauth_headers():
    return [(b"authorization", b"Bearer faketoken")]


class _FakeResp:
    def __init__(self, status=200, headers=None, content=b"ok"):
        self.status_code = status
        self.headers = headers or {}
        self._content = content
        self.aclosed = False

    async def aread(self):
        return self._content

    def aiter_bytes(self):
        async def gen():
            yield self._content

        return gen()

    async def aclose(self):
        self.aclosed = True


class _FakeClient:
    """Counting fake AsyncClient mirroring the httpx surface the proxy touches."""

    def __init__(self):
        self.aclosed = False
        self.responses: list[_FakeResp] = []
        self.send_timeouts = []
        self.request_timeouts = []

    def build_request(self, method, url, content=None, headers=None, timeout=None):
        self.send_timeouts.append(timeout)
        return {"url": url, "method": method, "timeout": timeout}

    async def send(self, req, stream=False):
        return self.responses.pop(0)

    async def request(self, method, url, content=None, headers=None, timeout=None):
        self.request_timeouts.append(timeout)
        return self.responses.pop(0)

    async def aclose(self):
        self.aclosed = True


class _Driver:
    """Bare ASGI driver so tests observe the real proxy end-to-end."""

    def __init__(self, app, method="GET", path="/mcp", body=b""):
        self.app = app
        self.method = method
        self.path = path
        self._body = body
        self.status = None
        self.body_buf = b""
        self.done = asyncio.Event()
        self.sent = []

    async def receive(self):
        return {"type": "http.request", "body": self._body, "more_body": False}

    async def send(self, message):
        self.sent.append(message)
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body_buf += message.get("body", b"")
            if not message.get("more_body", False):
                self.done.set()

    async def run(self):
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": "1.1",
            "method": self.method,
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": _oauth_headers(),
            "server": ("test", 80),
            "client": ("test", 1),
        }
        await self.app(scope, self.receive, self.send)


async def _drive(app, method="GET", path="/mcp", body=b""):
    driver = _Driver(app, method=method, path=path, body=body)
    await driver.run()
    return driver


@asynccontextmanager
async def _lifespan_app(srv, client):
    """Install a fake shared client, run startup, yield, then shutdown."""
    srv._UPSTREAM_CLIENT = client
    try:
        await srv._startup_upstream_client()
        yield
    finally:
        await srv._shutdown_upstream_client()


def test_shared_client_returned_by_getter_and_reused(srv, monkeypatch):
    # The getter must be sync and return the same process-level shared object.
    client = _FakeClient()
    srv._UPSTREAM_CLIENT = client

    got1 = srv._make_upstream_client(300.0)
    got2 = srv._make_upstream_client(300.0)
    assert got1 is client is got2
    # A sync getter must not require awaiting (the proxy calls it plainly).
    assert not asyncio.iscoroutinefunction(srv._make_upstream_client)

    assert srv._UPSTREAM_CLIENT is client
    assert not client.aclosed


def test_shared_client_closed_once_at_shutdown(srv):
    client = _FakeClient()
    srv._UPSTREAM_CLIENT = client

    async def scenario():
        try:
            await srv._startup_upstream_client()
            await srv._startup_upstream_client()  # idempotent
        finally:
            await srv._shutdown_upstream_client()

    asyncio.run(scenario())
    assert client.aclosed
    assert srv._UPSTREAM_CLIENT is None


@pytest.mark.asyncio
async def test_many_requests_share_one_client_and_only_resp_closed(srv, monkeypatch):
    """Across many proxy requests the same shared client is used and never
    closed by an individual request; only the upstream Response is released."""
    client = _FakeClient()
    srv._UPSTREAM_CLIENT = client
    responses = [_FakeResp(200, {}, b"r%d" % i) for i in range(6)]
    # send() pops a fresh response per call
    client.responses = responses.copy()

    app = srv.create_proxy_app()
    # run several requests through the same shared client
    for _ in range(6):
        driver = await _drive(app, "GET", "/mcp")
        assert driver.status == 200
    # the getter hit the same shared object for all 6 (no re-creation)
    assert not client.aclosed
    assert srv._UPSTREAM_CLIENT is client
    # but each upstream response was released
    assert all(resp.aclosed for resp in responses)


@pytest.mark.asyncio
async def test_timeout_override_applied_per_request(srv, monkeypatch):
    """Even with a finite shared-client default, every hop (POST and GET SSE)
    keeps the read=None timeouts attached to the built Request (AUTH-2)."""
    client = _FakeClient()

    async def _post_and_stream():
        client.responses = [  # POST -> GET stream
            _FakeResp(200, {}, b'{"ok":1}'),
            _FakeResp(200, {}, b"data: one\n\n"),
        ]
        d1 = await _drive(srv.create_proxy_app(), "POST", "/mcp", body=b'{"id":1}')
        d2 = await _drive(srv.create_proxy_app(), "GET", "/mcp")
        return d1, d2

    srv._UPSTREAM_CLIENT = client
    d1, d2 = await _post_and_stream()

    assert d1.status == 200 and d2.status == 200
    # both hops are stream=True sends; the timeout is attached via build_request
    post_t = client.send_timeouts[0]
    get_t = client.send_timeouts[1]
    for t in (post_t, get_t):
        assert isinstance(t, httpx.Timeout)
        assert t.read is None  # SSE keeps read=None (AUTH-2)
        assert isinstance(t.connect, float) and t.connect > 0
        assert isinstance(t.write, float) and t.write > 0

@pytest.mark.asyncio
async def test_concurrent_requests_reuse_single_client(srv, monkeypatch):
    """High concurrency through one shared client: all responses handled,
    the shared client is not closed, all responses released."""
    client = _FakeClient()
    srv._UPSTREAM_CLIENT = client
    N = 32
    responses = [_FakeResp(200, {}, b"x") for _ in range(N)]
    client.responses = responses.copy()

    app = srv.create_proxy_app()
    results = await asyncio.gather(*[_drive(app, "GET", "/mcp") for _ in range(N)])
    assert all(r.status == 200 for r in results)
    assert not client.aclosed
    assert srv._UPSTREAM_CLIENT is client
    assert all(resp.aclosed for resp in responses)


@pytest.mark.asyncio
async def test_401_and_connect_error_do_not_close_shared_client(srv, monkeypatch):
    client = _FakeClient()
    srv._UPSTREAM_CLIENT = client

    # 401 pass-through
    client.responses = [_FakeResp(401, {"WWW-Authenticate": 'Bearer realm="mcp"'}, b"nope")]
    d1 = await _drive(srv.create_proxy_app(), "GET", "/mcp")
    assert d1.status == 401
    assert not client.aclosed

    # pre-header connect error leads to 502; shared client still alive
    boom = _FakeClient()
    srv._UPSTREAM_CLIENT = boom

    async def raise_send(req, stream=False):
        raise httpx.ConnectError("connection refused")

    boom.send = raise_send
    d2 = await _drive(srv.create_proxy_app(), "GET", "/mcp")
    assert d2.status == 502
    assert not boom.aclosed


# --- BLOCKING REVIEW GATE: explicit timeout matrix -------------------------
# Deterministic proof that timeout selection is method+path aware, not a blind
# "everything read=None". GET/POST /mcp are SSE streamable hops (read=None);
# DELETE /mcp (session termination) and any non-MCP route stay FINITE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_matrix_get_and_post_mcp_read_none(srv, monkeypatch):
    """GET /mcp and POST /mcp are SSE streamable hops -> read=None (GATES 1,2)."""
    for method, path in (("GET", "/mcp"), ("POST", "/mcp")):
        client = _FakeClient()
        srv._UPSTREAM_CLIENT = client
        client.responses = [_FakeResp(200, {}, b"x")]
        d = await _drive(srv.create_proxy_app(), method, path)
        assert d.status == 200
        t = client.send_timeouts[0]
        assert isinstance(t, httpx.Timeout)
        assert t.read is None  # unbounded read for MCP SSE hop
        assert isinstance(t.connect, float) and t.connect > 0
        assert isinstance(t.write, float) and t.write > 0
        assert isinstance(t.pool, float) and t.pool > 0


@pytest.mark.asyncio
async def test_timeout_matrix_delete_mcp_stays_bounded(srv, monkeypatch):
    """DELETE /mcp terminates a session -> FINITE read, not read=None (GATE 3)."""
    client = _FakeClient()
    srv._UPSTREAM_CLIENT = client
    client.responses = [_FakeResp(204, {}, b"")]
    d = await _drive(srv.create_proxy_app(), "DELETE", "/mcp")
    assert d.status == 204
    t = client.send_timeouts[0]
    assert isinstance(t, httpx.Timeout)
    assert t.read is not None
    assert isinstance(t.read, float) and t.read > 0


@pytest.mark.asyncio
async def test_timeout_matrix_non_mcp_route_stays_bounded(srv, monkeypatch):
    """Any non-MCP route (other path) keeps the finite contract (GATE 4)."""
    for method in ("GET", "POST", "DELETE"):
        client = _FakeClient()
        srv._UPSTREAM_CLIENT = client
        client.responses = [_FakeResp(200, {}, b"x")]
        d = await _drive(srv.create_proxy_app(), method, "/health")
        assert d.status == 200
        t = client.send_timeouts[0]
        assert isinstance(t, httpx.Timeout)
        assert t.read is not None
        assert isinstance(t.read, float) and t.read > 0


@pytest.mark.asyncio
async def test_dcr_proxy_trace_visible_with_production_warning_log_level(
    srv, capsys, monkeypatch
):
    """Actual /register emits flushed stderr even when INFO logs are suppressed."""
    monkeypatch.setattr(srv.logger, "level", 30)  # standard root WARNING
    client = _FakeClient()
    client.responses = [
        _FakeResp(201, {"content-type": "application/json"}, b'{"client_id":"opaque"}')
    ]
    srv._UPSTREAM_CLIENT = client
    marker = b'{"client_secret":"do-not-echo-secret","client_name":"private-name"}'
    response = await _drive(srv.create_proxy_app(), "POST", "/register", body=marker)
    assert response.status == 201
    stderr = capsys.readouterr().err
    traces = [line for line in stderr.splitlines() if line.startswith("DCR_TRACE ")]
    matches = [
        re.fullmatch(
            r"DCR_TRACE id=([a-f0-9]{12}) phase=([a-z_]+) elapsed_ms=(\d+)", line
        )
        for line in traces
    ]
    assert len(matches) == 4 and all(match is not None for match in matches)
    assert [match.group(2) for match in matches] == [
        "received", "body_read", "upstream_send", "upstream_headers"
    ]
    assert len({match.group(1) for match in matches}) == 1
    assert [int(match.group(3)) for match in matches] == sorted(
        int(match.group(3)) for match in matches
    )
    assert "do-not-echo-secret" not in stderr
    assert "private-name" not in stderr
    assert "client_secret" not in stderr


@pytest.mark.asyncio
async def test_dcr_proxy_trace_marks_upstream_failure_without_log_leak(
    srv, capsys, monkeypatch
):
    class BrokenClient(_FakeClient):
        async def send(self, req, stream=False):
            raise httpx.ConnectError("upstream-secret-password")

    monkeypatch.setattr(srv.logger, "level", 30)
    srv._UPSTREAM_CLIENT = BrokenClient()
    response = await _drive(
        srv.create_proxy_app(), "POST", "/register", body=b'{"token":"private-token"}'
    )
    assert response.status == 502
    stderr = capsys.readouterr().err
    traces = [line for line in stderr.splitlines() if line.startswith("DCR_TRACE ")]
    assert [re.search(r"phase=([a-z_]+)", line).group(1) for line in traces] == [
        "received", "body_read", "upstream_send", "upstream_error"
    ]
    assert "upstream-secret-password" not in stderr
    assert "private-token" not in stderr


def test_dcr_trace_rejects_untrusted_identifier_and_phase_without_output(srv, capsys):
    with pytest.raises(ValueError, match="identifier"):
        srv._log_dcr_proxy_phase("bad\ncredentials", "received", 0.0)
    with pytest.raises(ValueError, match="phase"):
        srv._log_dcr_proxy_phase("a" * 12, "token", 0.0)
    assert capsys.readouterr().err == ""


async def _count_admitted_real_streams(srv, attempts: int) -> int:
    """Exercise the production httpx/httpcore pool against held TCP responses."""
    release = asyncio.Event()

    async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )
            await writer.drain()
            await release.wait()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_connection, "127.0.0.1", 0)
    assert server.sockets
    port = server.sockets[0].getsockname()[1]
    responses: list[httpx.Response] = []
    admitted = 0

    srv._UPSTREAM_CLIENT = None
    await srv._startup_upstream_client()
    request = type("RequestStub", (), {"method": "GET"})()
    try:
        for _ in range(attempts):
            upstream = await srv._proxy_upstream(
                request,
                f"http://127.0.0.1:{port}/mcp",
                b"",
                {},
                target_path="/mcp",
            )
            if upstream is None:
                break
            _, response = upstream
            responses.append(response)
            admitted += 1
    finally:
        for response in responses:
            await response.aclose()
        release.set()
        await srv._shutdown_upstream_client()
        server.close()
        await server.wait_closed()

    return admitted


@pytest.mark.asyncio
async def test_real_pool_admits_80_simultaneously_held_mcp_streams(srv, monkeypatch):
    """The real bounded pool must admit 80 held streams for ~40 two-hop transports."""
    original_timeout = srv._mcp_proxy_timeout

    def fast_pool_timeout(method: str, target_path: str) -> httpx.Timeout:
        timeout = original_timeout(method, target_path)
        return httpx.Timeout(
            connect=timeout.connect,
            write=timeout.write,
            pool=0.05,
            read=timeout.read,
        )

    monkeypatch.setattr(srv, "_mcp_proxy_timeout", fast_pool_timeout)

    admitted = await _count_admitted_real_streams(srv, 80)

    assert srv._UPSTREAM_MAX_CONNECTIONS >= 80
    assert admitted == 80


@pytest.mark.asyncio
async def test_real_pool_capacity_gate_is_sensitive_to_twenty_connection_mutation(srv, monkeypatch):
    """TEST-09: restoring the old 20-connection cap makes request 21 fail."""
    original_timeout = srv._mcp_proxy_timeout

    def fast_pool_timeout(method: str, target_path: str) -> httpx.Timeout:
        timeout = original_timeout(method, target_path)
        return httpx.Timeout(
            connect=timeout.connect,
            write=timeout.write,
            pool=0.05,
            read=timeout.read,
        )

    monkeypatch.setattr(srv, "_mcp_proxy_timeout", fast_pool_timeout)
    monkeypatch.setattr(srv, "_UPSTREAM_MAX_CONNECTIONS", 20)

    admitted = await _count_admitted_real_streams(srv, 21)

    assert admitted == 20


def test_dcr_register_upstream_budget_is_shorter_than_20s_client_stage(srv):
    """The OAuth client gives up after 20s; no proxy DCR hop may run for 300s."""
    timeout = srv._mcp_proxy_timeout("POST", "/register")
    assert 0 < timeout.read < 20
    assert 0 < srv._DCR_UPSTREAM_WALL_CLOCK_SECONDS < 20
    # SSE streams and non-DCR non-MCP operations retain their own contracts.
    assert srv._mcp_proxy_timeout("POST", "/mcp").read is None
    assert srv._mcp_proxy_timeout("GET", "/register").read == 300


@pytest.mark.asyncio
async def test_real_dcr_header_stall_releases_proxy_and_allows_next_request(srv, monkeypatch):
    """One hung DCR header must cancel promptly without poisoning the shared pool."""
    blocker = asyncio.Event()
    connections = 0

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        current = connections
        try:
            await reader.readuntil(b"\r\n\r\n")
            if current == 1:
                await blocker.wait()
            else:
                writer.write(
                    b"HTTP/1.1 201 Created\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 2\r\n"
                    b"Connection: close\r\n\r\n{}"
                )
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    assert server.sockets
    port = server.sockets[0].getsockname()[1]
    srv._UPSTREAM_CLIENT = httpx.AsyncClient(trust_env=False)
    monkeypatch.setattr(srv, "_DCR_UPSTREAM_WALL_CLOCK_SECONDS", 0.15)
    request = type("RequestStub", (), {"method": "POST"})()
    started = asyncio.get_running_loop().time()
    try:
        failed = await asyncio.wait_for(
            srv._proxy_upstream(
                request, f"http://127.0.0.1:{port}/register", b"{}",
                {"Content-Type": "application/json"},
                target_path="/register", dcr_trace_id="abcdef012345",
                dcr_started=started,
            ),
            timeout=2.0,
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert failed is None
        assert elapsed < 1.0
        recovered = await asyncio.wait_for(
            srv._proxy_upstream(
                request, f"http://127.0.0.1:{port}/register", b"{}",
                {"Content-Type": "application/json"},
                target_path="/register", dcr_trace_id="123456abcdef",
                dcr_started=started,
            ),
            timeout=2.0,
        )
        assert recovered is not None
        _, response = recovered
        assert response.status_code == 201
        await response.aclose()
        assert connections >= 2
    finally:
        blocker.set()
        await srv._UPSTREAM_CLIENT.aclose()
        srv._UPSTREAM_CLIENT = None
        server.close()
        await server.wait_closed()

