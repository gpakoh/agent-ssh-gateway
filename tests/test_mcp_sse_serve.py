"""Tests for scripts/mcp_sse_serve.py — private SSE MCP transport entrypoint.

Covers config parsing/fail-fast, the independent bearer-auth middleware,
empirical route discovery against the real FastMCP instance, safe-mode
enforcement, and that the bearer token never appears in stdout/stderr.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER_DIR = ROOT / "examples" / "mcp_server"
for _p in (str(MCP_SERVER_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.mcp_sse_serve import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    BearerAuthMiddleware,
    ConfigError,
    OriginValidationMiddleware,
    build_app,
    build_inner_app,
    discover_routes,
    is_non_loopback_allowed,
    is_origin_allowed,
    parse_allowed_origins,
    require_bearer_token,
    require_safe_mode,
    resolve_host,
    resolve_port,
    validate_bind_host,
)

SAFE_MODE_ENV = {
    "MCP_GATEWAY_TOOL_MODE": "mcp_client",
    "MCP_CLIENT_SAFE_MODE": "true",
    "MCP_ACCESS_PROFILE": "mcp_client_safe",
}


def _reload_gateway_server() -> None:
    """Force examples.mcp_server.server to re-run its module-level tool
    registration under the currently patched env vars. Needed because the
    module is a process-wide singleton and other test files may have
    already imported it under different env vars.
    """
    import examples.mcp_server.server as srv

    importlib.reload(srv)


# ---------------------------------------------------------------------------
# Config parsing / fail-fast
# ---------------------------------------------------------------------------


class TestResolveHost:
    def test_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_HTTP_HOST", None)
            assert resolve_host() == DEFAULT_HOST == "127.0.0.1"

    def test_explicit(self):
        with patch.dict(os.environ, {"MCP_HTTP_HOST": "localhost"}):
            assert resolve_host() == "localhost"

    def test_blank_falls_back_to_default(self):
        with patch.dict(os.environ, {"MCP_HTTP_HOST": "   "}):
            assert resolve_host() == DEFAULT_HOST


class TestResolvePort:
    def test_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_HTTP_PORT", None)
            assert resolve_port() == DEFAULT_PORT == 8086

    def test_explicit(self):
        with patch.dict(os.environ, {"MCP_HTTP_PORT": "9999"}):
            assert resolve_port() == 9999

    def test_invalid_raises_config_error(self):
        with patch.dict(os.environ, {"MCP_HTTP_PORT": "not-a-port"}):
            with pytest.raises(ConfigError):
                resolve_port()


class TestValidateBindHost:
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_always_allowed(self, host):
        validate_bind_host(host, allow_non_loopback=False)
        validate_bind_host(host, allow_non_loopback=True)

    def test_non_loopback_rejected_by_default(self):
        with pytest.raises(ConfigError, match="non-loopback"):
            validate_bind_host("0.0.0.0", allow_non_loopback=False)

    def test_non_loopback_allowed_with_explicit_flag(self):
        validate_bind_host("0.0.0.0", allow_non_loopback=True)


class TestIsNonLoopbackAllowed:
    def test_default_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_HTTP_ALLOW_NON_LOOPBACK", None)
            assert is_non_loopback_allowed() is False

    def test_requires_exact_true(self):
        with patch.dict(os.environ, {"MCP_HTTP_ALLOW_NON_LOOPBACK": "1"}):
            assert is_non_loopback_allowed() is False
        with patch.dict(os.environ, {"MCP_HTTP_ALLOW_NON_LOOPBACK": "true"}):
            assert is_non_loopback_allowed() is True


class TestRequireBearerToken:
    def test_missing_raises(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_HTTP_BEARER_TOKEN", None)
            with pytest.raises(ConfigError, match="MCP_HTTP_BEARER_TOKEN"):
                require_bearer_token()

    def test_present_returns_value(self):
        with patch.dict(os.environ, {"MCP_HTTP_BEARER_TOKEN": "spike-token-abc"}):
            assert require_bearer_token() == "spike-token-abc"


class TestRequireSafeMode:
    def test_wrong_tool_mode_raises(self):
        with patch.dict(os.environ, {"MCP_GATEWAY_TOOL_MODE": "standard", "MCP_CLIENT_SAFE_MODE": "true"}):
            with pytest.raises(ConfigError, match="MCP_GATEWAY_TOOL_MODE"):
                require_safe_mode()

    def test_safe_mode_false_raises(self):
        with patch.dict(os.environ, {"MCP_GATEWAY_TOOL_MODE": "mcp_client", "MCP_CLIENT_SAFE_MODE": "false"}):
            with pytest.raises(ConfigError, match="MCP_CLIENT_SAFE_MODE"):
                require_safe_mode()

    def test_safe_mode_missing_raises(self):
        with patch.dict(os.environ, {"MCP_GATEWAY_TOOL_MODE": "mcp_client"}, clear=False):
            os.environ.pop("MCP_CLIENT_SAFE_MODE", None)
            with pytest.raises(ConfigError, match="MCP_CLIENT_SAFE_MODE"):
                require_safe_mode()

    def test_safe_mode_true_passes(self):
        with patch.dict(os.environ, SAFE_MODE_ENV):
            require_safe_mode()  # must not raise


# ---------------------------------------------------------------------------
# Bearer auth middleware (isolated trivial app — fast, no gateway import)
# ---------------------------------------------------------------------------


def _trivial_app() -> Starlette:
    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/probe", ok)])


class TestBearerAuthMiddleware:
    def _client(self, token: str) -> TestClient:
        app = BearerAuthMiddleware(_trivial_app(), token)
        return TestClient(app)

    def test_missing_header_rejected(self):
        client = self._client("real-token")
        resp = client.get("/probe")
        assert resp.status_code == 401

    def test_wrong_token_rejected(self):
        client = self._client("real-token")
        resp = client.get("/probe", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401

    def test_malformed_header_rejected(self):
        client = self._client("real-token")
        resp = client.get("/probe", headers={"Authorization": "real-token"})
        assert resp.status_code == 401

    def test_correct_token_passes(self):
        client = self._client("real-token")
        resp = client.get("/probe", headers={"Authorization": "Bearer real-token"})
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_401_body_does_not_echo_provided_token(self):
        client = self._client("real-token")
        resp = client.get("/probe", headers={"Authorization": "Bearer some-leaked-looking-value"})
        assert resp.status_code == 401
        assert "some-leaked-looking-value" not in resp.text


# ---------------------------------------------------------------------------
# Route discovery + integration against the real FastMCP instance
# ---------------------------------------------------------------------------


class TestRouteDiscovery:
    def test_discovers_sse_and_messages_routes(self):
        with patch.dict(os.environ, SAFE_MODE_ENV):
            _reload_gateway_server()
            app = build_inner_app()
            routes = discover_routes(app)

        paths = {path for _, path in routes}
        # Empirical finding (Phase 16A plan): FastMCP registers /sse and
        # /messages by default — NOT a mount_path-derived path. Do not
        # assume the spec's pseudocode path without this check.
        assert "/sse" in paths
        assert "/messages" in paths


class TestBuildAppIntegration:
    """End-to-end: real 84-tool app, wrapped in BearerAuthMiddleware,
    both SSE and message endpoints must require the bearer token.
    """

    def _build(self, token: str = "integration-token"):
        env = dict(SAFE_MODE_ENV)
        env["MCP_HTTP_BEARER_TOKEN"] = token
        with patch.dict(os.environ, env):
            _reload_gateway_server()
            app, host, port = build_app()
        return app, host, port

    def test_safe_mode_enforced_in_real_toolset(self):
        _app, _host, _port = self._build()
        with patch.dict(os.environ, SAFE_MODE_ENV):
            _reload_gateway_server()
            import examples.mcp_server.server as srv

            assert len(srv.mcp._tool_manager._tools) == 73

    def test_fastmcp_own_auth_is_unwired(self):
        """Regression guard for a real bug found while building the PR2
        SSE smoke script: with MCP_AUTH_MODE unset, the default "oauth"
        wires a real GatewayOAuthProvider + AuthSettings into FastMCP,
        which then rejects MCP_HTTP_BEARER_TOKEN with its own
        RequireAuthMiddleware (a real subprocess+curl reproduction
        showed a 401 with an `invalid_token` body that did not come
        from BearerAuthMiddleware at all — the request passed our
        check and was rejected one layer further in). build_inner_app()
        must force MCP_AUTH_MODE=token so mcp.settings.auth stays None
        and BearerAuthMiddleware is the sole enforcement layer.
        """
        env = dict(SAFE_MODE_ENV)
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("MCP_AUTH_MODE", None)
            build_inner_app()
            import examples.mcp_server.server as srv

            assert srv.mcp.settings.auth is None

    def test_forces_token_mode_even_if_caller_set_oauth(self):
        """The override is unconditional (not setdefault): even if the
        caller's environment explicitly sets MCP_AUTH_MODE=oauth (e.g.
        copied from the stdio setup), this entrypoint must still force
        token mode so FastMCP's own auth never gets wired.
        """
        env = {**SAFE_MODE_ENV, "MCP_AUTH_MODE": "oauth"}
        with patch.dict(os.environ, env):
            build_inner_app()
            import examples.mcp_server.server as srv

            assert srv.mcp.settings.auth is None
            assert os.environ["MCP_AUTH_MODE"] == "token"

    def test_defaults_are_loopback(self):
        _app, host, _port = self._build()
        assert host == "127.0.0.1"

    def test_sse_endpoint_rejects_without_token(self):
        app, _host, _port = self._build(token="secret-xyz")
        client = TestClient(app)
        resp = client.get("/sse")
        assert resp.status_code == 401

    def test_sse_endpoint_rejects_wrong_token(self):
        app, _host, _port = self._build(token="secret-xyz")
        client = TestClient(app)
        resp = client.get("/sse", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_message_endpoint_rejects_without_token(self):
        app, _host, _port = self._build(token="secret-xyz")
        client = TestClient(app)
        # Mounted sub-app — any path under /messages must still be gated.
        resp = client.post("/messages/", json={})
        assert resp.status_code == 401

    def test_config_error_when_token_missing(self):
        env = dict(SAFE_MODE_ENV)
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("MCP_HTTP_BEARER_TOKEN", None)
            with pytest.raises(ConfigError, match="MCP_HTTP_BEARER_TOKEN"):
                build_app()

    def test_config_error_when_safe_mode_off(self):
        env = {"MCP_GATEWAY_TOOL_MODE": "mcp_client", "MCP_CLIENT_SAFE_MODE": "false",
                "MCP_HTTP_BEARER_TOKEN": "tok"}
        with patch.dict(os.environ, env):
            with pytest.raises(ConfigError, match="MCP_CLIENT_SAFE_MODE"):
                build_app()


# ---------------------------------------------------------------------------
# Token never appears in stdout/stderr
# ---------------------------------------------------------------------------


class TestNoTokenInOutput:
    def test_failure_path_does_not_print_token(self):
        """Even if a caller mistakenly sets the token while safe mode is
        off, the resulting fail-fast error must not echo the token value.
        """
        env = {
            "MCP_GATEWAY_TOOL_MODE": "mcp_client",
            "MCP_CLIENT_SAFE_MODE": "false",
            "MCP_HTTP_BEARER_TOKEN": "must-not-appear-in-output",
        }
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            from scripts.mcp_sse_serve import main

            exit_code = main()

        assert exit_code == 1
        assert "must-not-appear-in-output" not in out.getvalue()
        assert "must-not-appear-in-output" not in err.getvalue()

    def test_success_path_start_message_does_not_print_token(self, monkeypatch):
        """main() logs a startup line before calling uvicorn.run — patch
        uvicorn.run out so the test doesn't actually bind a socket, and
        assert the token isn't in the startup message.
        """
        env = dict(SAFE_MODE_ENV)
        env["MCP_HTTP_BEARER_TOKEN"] = "must-not-leak-either"

        called = {}

        def fake_run(app, host, port):
            called["host"] = host
            called["port"] = port

        monkeypatch.setattr("uvicorn.run", fake_run)

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            _reload_gateway_server()
            from scripts.mcp_sse_serve import main

            exit_code = main()

        assert exit_code == 0
        assert called["host"] == "127.0.0.1"
        assert "must-not-leak-either" not in out.getvalue()
        assert "must-not-leak-either" not in err.getvalue()


# ---------------------------------------------------------------------------
# Origin validation (Phase 17B)
# ---------------------------------------------------------------------------


class TestParseAllowedOrigins:
    def test_empty_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_HTTP_ALLOWED_ORIGINS", None)
            assert parse_allowed_origins() == frozenset()

    def test_parses_comma_separated(self):
        with patch.dict(os.environ, {"MCP_HTTP_ALLOWED_ORIGINS": "http://a.local, http://b.local"}):
            assert parse_allowed_origins() == frozenset({"http://a.local", "http://b.local"})


class TestIsOriginAllowed:
    def test_missing_origin_allowed(self):
        assert is_origin_allowed(None, frozenset()) is True
        assert is_origin_allowed("", frozenset()) is True

    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:9999",
            "http://localhost:9999",
            "http://[::1]:9999",
            "http://127.0.0.1",
        ],
    )
    def test_loopback_origin_allowed_by_default(self, origin):
        assert is_origin_allowed(origin, frozenset()) is True

    def test_non_loopback_rejected_by_default(self):
        assert is_origin_allowed("http://evil.example.com", frozenset()) is False

    def test_explicit_extra_origin_allowed(self):
        extra = frozenset({"http://custom.local:1234"})
        assert is_origin_allowed("http://custom.local:1234", extra) is True

    def test_extra_origin_does_not_allow_others(self):
        extra = frozenset({"http://custom.local:1234"})
        assert is_origin_allowed("http://still-not-allowed.local", extra) is False


class TestOriginValidationMiddleware:
    def _client(self, extra_allowed: frozenset[str] = frozenset()) -> TestClient:
        app = OriginValidationMiddleware(_trivial_app(), extra_allowed)
        return TestClient(app)

    def test_no_origin_passes(self):
        client = self._client()
        resp = client.get("/probe")
        assert resp.status_code == 200

    def test_loopback_origin_passes(self):
        client = self._client()
        resp = client.get("/probe", headers={"Origin": "http://127.0.0.1:9999"})
        assert resp.status_code == 200

    def test_non_loopback_origin_rejected(self):
        client = self._client()
        resp = client.get("/probe", headers={"Origin": "http://evil.example.com"})
        assert resp.status_code == 403

    def test_extra_allowed_origin_passes(self):
        client = self._client(frozenset({"http://custom.local:1234"}))
        resp = client.get("/probe", headers={"Origin": "http://custom.local:1234"})
        assert resp.status_code == 200

    def test_rejection_does_not_echo_full_origin_in_body(self):
        client = self._client()
        resp = client.get("/probe", headers={"Origin": "http://evil-looking-value.example.com"})
        assert resp.status_code == 403
        assert "evil-looking-value.example.com" not in resp.text


class TestStackedMiddlewareSuccessPaths:
    """Proves OriginValidationMiddleware + BearerAuthMiddleware, stacked
    exactly as build_app() stacks them, let a well-formed request through
    to the wrapped app.

    Uses a trivial inner app rather than the real 84-tool SSE app:
    Starlette's TestClient cannot fully drive a real SSE connect (the
    mcp SDK's own SseServerTransport validates the HTTP Host header
    deep inside connect_sse(), and TestClient's synthetic "testserver"
    Host does not match any loopback pattern — a TestClient limitation
    already known from Phase 16B PR1, not a bug in this middleware).
    The equivalent real, end-to-end success path (real subprocess, real
    socket, real Host header) is exercised by
    scripts/mcp_sse_safe_smoke.py and was additionally verified manually
    against a real subprocess for all three cases below while building
    this test file.
    """

    def _client(self, token: str, extra_allowed: frozenset[str] = frozenset()) -> TestClient:
        app: Any = BearerAuthMiddleware(_trivial_app(), token)
        app = OriginValidationMiddleware(app, extra_allowed)
        return TestClient(app)

    def test_no_origin_correct_bearer_passes(self):
        client = self._client("tok-a")
        resp = client.get("/probe", headers={"Authorization": "Bearer tok-a"})
        assert resp.status_code == 200

    def test_loopback_origin_correct_bearer_passes(self):
        client = self._client("tok-b")
        resp = client.get(
            "/probe",
            headers={"Authorization": "Bearer tok-b", "Origin": "http://127.0.0.1:5555"},
        )
        assert resp.status_code == 200

    def test_allowed_custom_origin_from_env_passes(self):
        client = self._client("tok-c", frozenset({"http://custom.local:1234"}))
        resp = client.get(
            "/probe",
            headers={"Authorization": "Bearer tok-c", "Origin": "http://custom.local:1234"},
        )
        assert resp.status_code == 200


class TestBuildAppOriginIntegration:
    """End-to-end rejection paths against the real 84-tool app: these
    are rejected by OriginValidationMiddleware/BearerAuthMiddleware
    before ever reaching the mcp SDK's deeper connect_sse() Host check,
    so TestClient handles them fine (unlike the success paths above).
    """

    def _build(self, token: str = "origin-integration-token", allowed_origins: str = ""):
        env = dict(SAFE_MODE_ENV)
        env["MCP_HTTP_BEARER_TOKEN"] = token
        if allowed_origins:
            env["MCP_HTTP_ALLOWED_ORIGINS"] = allowed_origins
        with patch.dict(os.environ, env):
            _reload_gateway_server()
            app, host, port = build_app()
        return app, host, port

    def test_non_loopback_origin_rejected_with_403_regardless_of_bearer(self):
        app, _host, _port = self._build(token="tok-d")
        client = TestClient(app)
        resp_correct_token = client.get(
            "/sse",
            headers={"Authorization": "Bearer tok-d", "Origin": "http://evil.example.com"},
        )
        assert resp_correct_token.status_code == 403

        resp_wrong_token = client.get(
            "/sse",
            headers={"Authorization": "Bearer wrong", "Origin": "http://evil.example.com"},
        )
        assert resp_wrong_token.status_code == 403

    def test_wrong_bearer_with_loopback_origin_still_401(self):
        app, _host, _port = self._build(token="tok-e")
        client = TestClient(app)
        resp = client.get(
            "/sse",
            headers={"Authorization": "Bearer wrong", "Origin": "http://localhost:5555"},
        )
        assert resp.status_code == 401

    def test_messages_endpoint_protected_by_origin_too(self):
        app, _host, _port = self._build(token="tok-f")
        client = TestClient(app)
        resp = client.post(
            "/messages/",
            json={},
            headers={"Origin": "http://evil.example.com"},
        )
        assert resp.status_code == 403
