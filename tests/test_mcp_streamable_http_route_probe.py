"""Tests for scripts/mcp_streamable_http_route_probe.py (Phase 18B PR1).

Covers: routes/methods are discovered empirically against the real
FastMCP instance (not hardcoded from the MCP spec's prose), the probe
never requires a gateway credential or master key, the ephemeral
server never binds anything but loopback, and the probe's own printed
output never leaks a token/session-id-shaped secret. The real status
codes observed here are asserted as a fixed contract for PR2/PR3 to
build against.
"""

from __future__ import annotations

import importlib
import inspect
import io
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER_DIR = ROOT / "examples" / "mcp_server"
for _p in (str(MCP_SERVER_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.mcp_streamable_http_route_probe import (  # noqa: E402
    ConfigError,
    build_streamable_http_app,
    discover_methods,
    discover_routes,
    main,
    run_probe,
)

SAFE_MODE_ENV = {
    "MCP_GATEWAY_TOOL_MODE": "chatgpt",
    "MCP_CHATGPT_SAFE_MODE": "true",
    "MCP_ACCESS_PROFILE": "chatgpt_safe",
}

GATEWAY_CREDENTIAL_VARS = ("GATEWAY_URL", "GATEWAY_API_KEY", "GATEWAY_AGENT_TOKEN", "API_KEY")


def _reload_gateway_server() -> None:
    import examples.mcp_server.server as srv

    importlib.reload(srv)


class TestBuildStreamableHttpApp:
    def test_config_error_when_safe_mode_off(self):
        env = {"MCP_GATEWAY_TOOL_MODE": "chatgpt", "MCP_CHATGPT_SAFE_MODE": "false"}
        with patch.dict(os.environ, env):
            with pytest.raises(ConfigError, match="MCP_CHATGPT_SAFE_MODE"):
                build_streamable_http_app()

    def test_builds_without_any_gateway_credential(self):
        """The app-building step must not require a live gateway or any
        master-key-equivalent credential — only the (never-called-here)
        `health` tool would need one.
        """
        with patch.dict(os.environ, SAFE_MODE_ENV, clear=False):
            for var in GATEWAY_CREDENTIAL_VARS:
                os.environ.pop(var, None)
            _reload_gateway_server()
            app = build_streamable_http_app()

        assert app is not None
        assert hasattr(app, "routes")

    def test_fastmcp_own_auth_stays_unwired(self):
        """Same precondition PR2 will depend on for SSE-parity bearer
        auth to work: FastMCP's own RequireAuthMiddleware must not be
        wired, or it would reject a future bearer token with its own
        error path before this repo's middleware ever runs.
        """
        with patch.dict(os.environ, SAFE_MODE_ENV, clear=False):
            os.environ.pop("MCP_AUTH_MODE", None)
            build_streamable_http_app()
            import examples.mcp_server.server as srv

            assert srv.mcp.settings.auth is None


class TestDiscoverRoutesEmpirical:
    """The whole point of PR1: read the real route list off a real
    built app, rather than trusting the MCP spec's prose ("a single
    endpoint... e.g. /mcp") or the Phase 18A design doc's source-only
    claim.
    """

    def _build(self):
        with patch.dict(os.environ, SAFE_MODE_ENV):
            _reload_gateway_server()
            return build_streamable_http_app()

    def test_single_mcp_route_discovered(self):
        app = self._build()
        routes = discover_routes(app)
        paths = {path for _, path in routes}
        assert paths == {"/mcp"}

    def test_methods_are_unrestricted_on_the_route(self):
        """Starlette registers streamable_http_app()'s one route with a
        raw ASGI callable, not a function-based endpoint — so `methods`
        is None (any method reaches the session manager), not a fixed
        {"GET", "POST"} list. Assert the empirical value, not a guess.
        """
        app = self._build()
        methods = discover_methods(app)
        assert len(methods) == 1
        assert methods[0]["path"] == "/mcp"
        assert methods[0]["methods"] is None


class TestEphemeralProbeContract:
    """Full real-HTTP run: ephemeral uvicorn server, real requests, real
    responses. Slower than the unit tests above but this is the only
    way to honestly answer "what status code does this return" rather
    than assume it from the spec.
    """

    def test_never_binds_non_loopback(self):
        """Static check: the probe module must never construct a host
        value other than 127.0.0.1 anywhere in its server-starting path.
        """
        import scripts.mcp_streamable_http_route_probe as probe_mod

        source = inspect.getsource(probe_mod)
        assert "0.0.0.0" not in source
        assert '"127.0.0.1"' in source

    def test_run_probe_end_to_end_without_gateway_credentials(self):
        """This is the PR1 contract: exact status codes and route shape
        that PR2 (entrypoint) and PR3 (smoke test) must build against,
        established by a real subprocess-free but real-HTTP run — not
        assumed from the MCP spec's prose.
        """
        with patch.dict(os.environ, SAFE_MODE_ENV, clear=False):
            for var in GATEWAY_CREDENTIAL_VARS:
                os.environ.pop(var, None)
            _reload_gateway_server()
            evidence = run_probe()

        assert evidence["routes"] == [("Route", "/mcp")]
        assert evidence["methods"] == [
            {"route_class": "Route", "path": "/mcp", "methods": None}
        ]

        probe_results = evidence["probe_results"]
        assert set(probe_results) == {
            "GET /mcp",
            "POST /mcp (malformed body)",
            "DELETE /mcp (no session header)",
        }
        for label, result in probe_results.items():
            assert "error" not in result, f"{label} raised: {result}"
            # Empirically observed contract for PR2/PR3: every one of
            # these three requests is missing (or does not carry) a
            # valid Mcp-Session-Id for an already-initialized session,
            # so the SDK's own session manager rejects all three with
            # 400 Bad Request — not 405, not 200. PR2's bearer/Origin
            # middleware sits in front of this and must not change it.
            assert result["status"] == 400, f"{label}: {result}"

        # GET and POST responses got a Mcp-Session-Id header (the SDK
        # assigns a fresh session per request lacking one); DELETE with
        # no session header never reaches session assignment.
        assert probe_results["GET /mcp"]["mcp_session_id"]
        assert probe_results["POST /mcp (malformed body)"]["mcp_session_id"]

    def test_no_secrets_in_probe_output(self):
        """The probe's own stderr output must never contain a raw
        session-id-shaped value — only the redacted placeholder.
        """
        with patch.dict(os.environ, SAFE_MODE_ENV, clear=False):
            for var in GATEWAY_CREDENTIAL_VARS:
                os.environ.pop(var, None)
            _reload_gateway_server()
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = main()

        assert rc == 0
        combined = out.getvalue() + err.getvalue()
        assert "<REDACTED>" in combined

        secret_like = re.compile(r"[A-Za-z0-9_\-]{20,}")
        for match in secret_like.finditer(combined):
            assert match.group(0) == "REDACTED" or "REDACTED" in combined[
                max(0, match.start() - 12) : match.end()
            ], f"possible unredacted secret-shaped value: {match.group(0)!r}"
