"""Tests for scripts/mcp_streamable_http_serve.py — private Streamable
HTTP MCP transport entrypoint (Phase 18B PR2).

Mirrors tests/test_mcp_sse_serve.py's structure: config parsing/
fail-fast with this entrypoint's own env var names, reuse of
BearerAuthMiddleware/OriginValidationMiddleware from mcp_sse_serve.py
(imported, not duplicated), empirical route (/mcp, per Phase 18B PR1)
protection by both auth and Origin validation, that FastMCP's own auth
stays unwired, and that neither the bearer token nor the Origin header
value is ever printed.

Success paths (correct bearer, no/loopback/allowed-custom Origin) are
tested against a trivial inner app, not the real Streamable HTTP app —
Starlette's TestClient sends a synthetic "testserver" Host header,
which the mcp SDK's own TransportSecurityMiddleware rejects with a 421
deep inside the session manager (confirmed by direct reproduction: a
real subprocess + real HTTP client, as used by
scripts/mcp_streamable_http_route_probe.py, does not hit this — it is
a TestClient-only artifact, the same class of limitation already
documented in tests/test_mcp_sse_serve.py for SSE's connect_sse()).
Rejection paths (wrong bearer, disallowed Origin) are tested against
the real 84-tool app, since those are rejected by this script's own
middleware before ever reaching the deep SDK-level Host check.
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

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER_DIR = ROOT / "examples" / "mcp_server"
for _p in (str(MCP_SERVER_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

from scripts.mcp_sse_serve import (  # noqa: E402
    BearerAuthMiddleware,
    ConfigError,
    OriginValidationMiddleware,
)
from scripts.mcp_streamable_http_serve import (  # noqa: E402
    ALLOW_NON_LOOPBACK_ENV_VAR,
    ALLOWED_ORIGINS_ENV_VAR,
    BEARER_TOKEN_ENV_VAR,
    DEFAULT_PORT,
    HOST_ENV_VAR,
    PORT_ENV_VAR,
    build_app,
    build_inner_app,
    is_non_loopback_allowed,
    parse_allowed_origins,
    require_bearer_token,
    resolve_host,
    resolve_port,
)

SAFE_MODE_ENV = {
    "MCP_GATEWAY_TOOL_MODE": "chatgpt",
    "MCP_CHATGPT_SAFE_MODE": "true",
    "MCP_ACCESS_PROFILE": "chatgpt_safe",
}


def _reload_gateway_server() -> None:
    import examples.mcp_server.server as srv

    importlib.reload(srv)


def _trivial_app() -> Starlette:
    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/probe", ok)])


# ---------------------------------------------------------------------------
# Env var names — distinct from SSE's, distinct default port
# ---------------------------------------------------------------------------


class TestEnvVarNames:
    def test_uses_its_own_env_var_names_not_sse(self):
        assert HOST_ENV_VAR == "MCP_STREAMABLE_HTTP_HOST"
        assert PORT_ENV_VAR == "MCP_STREAMABLE_HTTP_PORT"
        assert ALLOW_NON_LOOPBACK_ENV_VAR == "MCP_STREAMABLE_HTTP_ALLOW_NON_LOOPBACK"
        assert BEARER_TOKEN_ENV_VAR == "MCP_STREAMABLE_HTTP_BEARER_TOKEN"
        assert ALLOWED_ORIGINS_ENV_VAR == "MCP_STREAMABLE_HTTP_ALLOWED_ORIGINS"

    def test_default_port_does_not_collide_with_sse(self):
        from scripts.mcp_sse_serve import DEFAULT_PORT as SSE_DEFAULT_PORT

        assert DEFAULT_PORT != SSE_DEFAULT_PORT
        assert DEFAULT_PORT == 8087


# ---------------------------------------------------------------------------
# Config parsing / fail-fast
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    def test_default_host_is_loopback(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(HOST_ENV_VAR, None)
            assert resolve_host() == "127.0.0.1"

    def test_default_port_is_8087(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PORT_ENV_VAR, None)
            assert resolve_port() == 8087

    def test_port_overridable_via_own_env_var(self):
        with patch.dict(os.environ, {PORT_ENV_VAR: "9999"}):
            assert resolve_port() == 9999


class TestNonLoopbackBind:
    def test_non_loopback_rejected_by_default(self):
        env = dict(SAFE_MODE_ENV)
        env[BEARER_TOKEN_ENV_VAR] = "tok"
        env[HOST_ENV_VAR] = "0.0.0.0"
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop(ALLOW_NON_LOOPBACK_ENV_VAR, None)
            with pytest.raises(ConfigError, match="non-loopback"):
                build_app()

    def test_non_loopback_allowed_only_with_explicit_env(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ALLOW_NON_LOOPBACK_ENV_VAR, None)
            assert is_non_loopback_allowed() is False
        with patch.dict(os.environ, {ALLOW_NON_LOOPBACK_ENV_VAR: "true"}):
            assert is_non_loopback_allowed() is True
        with patch.dict(os.environ, {ALLOW_NON_LOOPBACK_ENV_VAR: "1"}):
            # Requires the exact string "true", same as SSE's guard.
            assert is_non_loopback_allowed() is False

    def test_non_loopback_error_names_its_own_override_var(self):
        """The error message must point at this entrypoint's own env
        var, not SSE's — otherwise an operator would set the wrong one.
        """
        env = dict(SAFE_MODE_ENV)
        env[BEARER_TOKEN_ENV_VAR] = "tok"
        env[HOST_ENV_VAR] = "0.0.0.0"
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop(ALLOW_NON_LOOPBACK_ENV_VAR, None)
            with pytest.raises(ConfigError, match=ALLOW_NON_LOOPBACK_ENV_VAR):
                build_app()


class TestRequireBearerToken:
    def test_config_error_when_token_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(BEARER_TOKEN_ENV_VAR, None)
            with pytest.raises(ConfigError, match=BEARER_TOKEN_ENV_VAR):
                require_bearer_token()

    def test_missing_token_fails_fast_in_build_app(self):
        env = dict(SAFE_MODE_ENV)
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop(BEARER_TOKEN_ENV_VAR, None)
            with pytest.raises(ConfigError, match=BEARER_TOKEN_ENV_VAR):
                build_app()


class TestSafeModeEnforced:
    def test_config_error_when_safe_mode_off(self):
        env = {
            "MCP_GATEWAY_TOOL_MODE": "chatgpt",
            "MCP_CHATGPT_SAFE_MODE": "false",
            BEARER_TOKEN_ENV_VAR: "tok",
        }
        with patch.dict(os.environ, env):
            with pytest.raises(ConfigError, match="MCP_CHATGPT_SAFE_MODE"):
                build_app()


class TestParseAllowedOrigins:
    def test_empty_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ALLOWED_ORIGINS_ENV_VAR, None)
            assert parse_allowed_origins() == frozenset()

    def test_parses_comma_separated(self):
        with patch.dict(os.environ, {ALLOWED_ORIGINS_ENV_VAR: "http://a.local, http://b.local"}):
            assert parse_allowed_origins() == frozenset({"http://a.local", "http://b.local"})


# ---------------------------------------------------------------------------
# Middleware reuse — success paths (trivial app, per the TestClient
# Host-header limitation documented above)
# ---------------------------------------------------------------------------


class TestStackedMiddlewareSuccessPaths:
    def _client(self, token: str, extra_allowed: frozenset[str] = frozenset()) -> TestClient:
        app: Any = BearerAuthMiddleware(_trivial_app(), token)
        app = OriginValidationMiddleware(app, extra_allowed)
        return TestClient(app)

    def test_correct_bearer_no_origin_passes(self):
        client = self._client("tok-a")
        resp = client.get("/probe", headers={"Authorization": "Bearer tok-a"})
        assert resp.status_code == 200

    def test_loopback_origin_passes(self):
        client = self._client("tok-b")
        resp = client.get(
            "/probe",
            headers={"Authorization": "Bearer tok-b", "Origin": "http://127.0.0.1:5555"},
        )
        assert resp.status_code == 200

    def test_custom_allowlisted_origin_passes(self):
        client = self._client("tok-c", frozenset({"http://custom.local:1234"}))
        resp = client.get(
            "/probe",
            headers={"Authorization": "Bearer tok-c", "Origin": "http://custom.local:1234"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Integration against the real FastMCP instance — rejection paths
# ---------------------------------------------------------------------------


class TestBuildAppIntegration:
    def _build(self, token: str = "integration-token", allowed_origins: str = ""):
        env = dict(SAFE_MODE_ENV)
        env[BEARER_TOKEN_ENV_VAR] = token
        if allowed_origins:
            env[ALLOWED_ORIGINS_ENV_VAR] = allowed_origins
        with patch.dict(os.environ, env):
            _reload_gateway_server()
            return build_app()

    def test_defaults_are_loopback(self):
        _app, host, port = self._build()
        assert host == "127.0.0.1"
        assert port == 8087

    def test_mcp_endpoint_rejects_without_token(self):
        app, _host, _port = self._build(token="secret-xyz")
        client = TestClient(app)
        resp = client.get("/mcp")
        assert resp.status_code == 401

    def test_mcp_endpoint_rejects_wrong_token(self):
        app, _host, _port = self._build(token="secret-xyz")
        client = TestClient(app)
        resp = client.get("/mcp", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_non_loopback_origin_rejected_with_403_regardless_of_bearer(self):
        app, _host, _port = self._build(token="tok-d")
        client = TestClient(app)
        resp_correct_token = client.get(
            "/mcp",
            headers={"Authorization": "Bearer tok-d", "Origin": "http://evil.example.com"},
        )
        assert resp_correct_token.status_code == 403

        resp_wrong_token = client.get(
            "/mcp",
            headers={"Authorization": "Bearer wrong", "Origin": "http://evil.example.com"},
        )
        assert resp_wrong_token.status_code == 403

    def test_wrong_bearer_with_loopback_origin_still_401(self):
        app, _host, _port = self._build(token="tok-e")
        client = TestClient(app)
        resp = client.get(
            "/mcp",
            headers={"Authorization": "Bearer wrong", "Origin": "http://localhost:5555"},
        )
        assert resp.status_code == 401

    def test_fastmcp_own_auth_is_unwired(self):
        """Same precondition as SSE: FastMCP's own RequireAuthMiddleware
        must not be wired, or a correct MCP_STREAMABLE_HTTP_BEARER_TOKEN
        would still be rejected one layer further in by FastMCP's own
        (OAuth-oriented) auth check.
        """
        env = dict(SAFE_MODE_ENV)
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("MCP_AUTH_MODE", None)
            build_inner_app()
            import examples.mcp_server.server as srv

            assert srv.mcp.settings.auth is None

    def test_forces_token_mode_even_if_caller_set_oauth(self):
        env = {**SAFE_MODE_ENV, "MCP_AUTH_MODE": "oauth"}
        with patch.dict(os.environ, env):
            build_inner_app()
            import examples.mcp_server.server as srv

            assert srv.mcp.settings.auth is None
            assert os.environ["MCP_AUTH_MODE"] == "token"


# ---------------------------------------------------------------------------
# No secrets in output
# ---------------------------------------------------------------------------


class TestNoTokenOrOriginInOutput:
    def test_failure_path_does_not_print_token(self):
        env = {
            "MCP_GATEWAY_TOOL_MODE": "chatgpt",
            "MCP_CHATGPT_SAFE_MODE": "false",
            BEARER_TOKEN_ENV_VAR: "must-not-appear-in-output",
        }
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            from scripts.mcp_streamable_http_serve import main

            exit_code = main()

        assert exit_code == 1
        assert "must-not-appear-in-output" not in out.getvalue()
        assert "must-not-appear-in-output" not in err.getvalue()

    def test_success_path_start_message_does_not_print_token(self, monkeypatch):
        env = dict(SAFE_MODE_ENV)
        env[BEARER_TOKEN_ENV_VAR] = "must-not-leak-either"

        called = {}

        def fake_run(app, host, port):
            called["host"] = host
            called["port"] = port

        monkeypatch.setattr("uvicorn.run", fake_run)

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            _reload_gateway_server()
            from scripts.mcp_streamable_http_serve import main

            exit_code = main()

        assert exit_code == 0
        assert called["host"] == "127.0.0.1"
        assert called["port"] == 8087
        assert "must-not-leak-either" not in out.getvalue()
        assert "must-not-leak-either" not in err.getvalue()

    def test_rejected_origin_response_body_does_not_echo_the_origin_value(self):
        """OriginValidationMiddleware's 403 body is a fixed
        `{"error": "origin_not_allowed"}` (see mcp_sse_serve.py) — it
        never echoes the request's Origin header back to the caller.
        Confirms this entrypoint's real app still gets that behavior.
        """
        env = dict(SAFE_MODE_ENV)
        env[BEARER_TOKEN_ENV_VAR] = "tok-g"
        with patch.dict(os.environ, env):
            _reload_gateway_server()
            app, _host, _port = build_app()

        client = TestClient(app)
        origin = "http://evil.example.com/should-not-be-echoed"
        resp = client.get(
            "/mcp",
            headers={"Authorization": "Bearer tok-g", "Origin": origin},
        )

        assert resp.status_code == 403
        assert origin not in resp.text
        assert "should-not-be-echoed" not in resp.text
