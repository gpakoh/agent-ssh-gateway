#!/usr/bin/env python3
"""Private SSE MCP transport entrypoint (Phase 16B PR1 spike).

Serves the existing examples/mcp_server FastMCP tool set over SSE,
bound to loopback by default, with a bearer-token layer enforced
independently of the still-unproven MCP_AUTH_MODE=token wiring.

Does not modify examples/mcp_server/server.py's stdio path (the module
is imported, not edited — see _force_fastmcp_auth_unwired() for the one
env var this process forces before that import). Does not add Docker
Compose / systemd wiring. Does not enable public bind by default —
MCP_HTTP_ALLOW_NON_LOOPBACK=true is required to relax the loopback-only
guard, and this script does not set it.

This process always forces MCP_AUTH_MODE=token in its own environment
before importing examples.mcp_server.server, regardless of what the
caller passed in. Without this, the default MCP_AUTH_MODE=oauth wires a
real FastMCP-native auth layer that rejects MCP_HTTP_BEARER_TOKEN with
its own error response — found via a real subprocess+curl reproduction
while building the Phase 16B PR2 smoke script.

Env vars:
  MCP_HTTP_HOST                default 127.0.0.1
  MCP_HTTP_PORT                default 8086
  MCP_HTTP_ALLOW_NON_LOOPBACK  must be exactly "true" to bind non-loopback
  MCP_HTTP_BEARER_TOKEN        required — protects the SSE + message endpoints
  MCP_CHATGPT_SAFE_MODE        must be "true"
  MCP_GATEWAY_TOOL_MODE        must be "chatgpt"

MCP_HTTP_BEARER_TOKEN is never logged or echoed back in responses.
"""

from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER_DIR = ROOT / "examples" / "mcp_server"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8086


class ConfigError(RuntimeError):
    """Raised when the runtime environment is not safe to start with."""


def _ensure_import_paths() -> None:
    """Mirror the sys.path setup used by tests/conftest.py and the other
    mcp_* scripts: examples/mcp_server uses bare intra-package imports
    (e.g. `from command_policy import ...`), and examples.mcp_server.server
    is imported as a dotted package from the repo root.
    """
    for p in (str(MCP_SERVER_DIR), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)


def resolve_host() -> str:
    return os.environ.get("MCP_HTTP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST


def resolve_port() -> int:
    raw = os.environ.get("MCP_HTTP_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"MCP_HTTP_PORT must be an integer, got {raw!r}") from exc


def validate_bind_host(host: str, allow_non_loopback: bool) -> None:
    """Fail fast unless host is loopback or the override is explicitly set."""
    if host in LOOPBACK_HOSTS:
        return
    if allow_non_loopback:
        return
    raise ConfigError(
        f"Refusing to bind MCP SSE server to non-loopback host {host!r}. "
        "Set MCP_HTTP_ALLOW_NON_LOOPBACK=true to override explicitly "
        "(not recommended outside a reviewed private-network deployment)."
    )


def is_non_loopback_allowed() -> bool:
    return os.environ.get("MCP_HTTP_ALLOW_NON_LOOPBACK", "").strip().lower() == "true"


def require_safe_mode() -> None:
    """Fail fast unless the server is configured for ChatGPT safe mode.

    Reuses examples.mcp_server.tool_modes as the single source of truth
    instead of re-implementing the env var parsing here.
    """
    _ensure_import_paths()
    from tool_modes import get_tool_mode, is_chatgpt_safe_mode  # noqa: PLC0415

    mode = get_tool_mode()
    if mode != "chatgpt":
        raise ConfigError(
            f"MCP_GATEWAY_TOOL_MODE must be 'chatgpt' for the private SSE "
            f"entrypoint, got {mode!r}."
        )
    if not is_chatgpt_safe_mode():
        raise ConfigError(
            "MCP_CHATGPT_SAFE_MODE must be true for the private SSE entrypoint."
        )


def require_bearer_token() -> str:
    token = os.environ.get("MCP_HTTP_BEARER_TOKEN", "")
    if not token:
        raise ConfigError("MCP_HTTP_BEARER_TOKEN is required to start the SSE entrypoint.")
    return token


class BearerAuthMiddleware:
    """Minimal ASGI middleware requiring `Authorization: Bearer <token>`.

    Wraps the whole app (not a per-route Starlette dependency), so both
    the SSE GET endpoint and the message POST endpoint are protected by
    the same check before any MCP routing happens. Independent of
    MCP_AUTH_MODE / GatewayOAuthProvider — does not assume that wiring
    is correct.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_auth = headers.get(b"authorization", b"").decode("latin-1")
        provided = raw_auth[len("Bearer ") :] if raw_auth.startswith("Bearer ") else ""

        if not hmac.compare_digest(provided, self._token):
            from starlette.responses import JSONResponse  # noqa: PLC0415

            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def discover_routes(app: Any) -> list[tuple[str, str | None]]:
    """Return (route_class_name, path) pairs for a Starlette app's routes.

    Used both by the route-discovery test and (optionally) for a quick
    `python3 scripts/mcp_sse_serve.py --routes` sanity check.
    """
    return [(type(r).__name__, getattr(r, "path", None)) for r in app.routes]


def _force_fastmcp_auth_unwired() -> None:
    """Force examples.mcp_server.server's MCP_AUTH_MODE to "token" before
    it is imported, so FastMCP's own auth stays unwired (mcp.settings.auth
    stays None — see the Phase 16A plan's finding #2).

    Without this, the default MCP_AUTH_MODE=oauth wires a real
    GatewayOAuthProvider + AuthSettings into FastMCP, which then rejects
    our MCP_HTTP_BEARER_TOKEN with its own RequireAuthMiddleware (it only
    recognizes tokens issued through its own OAuth/DCR flow) — discovered
    via a real subprocess+curl reproduction while building the PR2 smoke
    script, where "correct token" still returned 401 with an
    `invalid_token` body that did not come from BearerAuthMiddleware.

    MCP_PUBLIC_TOKEN here is a throwaway value only used to satisfy
    server.py's non-empty check for the (currently dead-for-HTTP, per
    finding #2) token-mode branch — it is NOT the credential that
    protects this entrypoint. MCP_HTTP_BEARER_TOKEN is.

    This is an unconditional override, not setdefault: this entrypoint's
    entire safety model depends on FastMCP's own auth never being wired,
    regardless of whatever MCP_AUTH_MODE a caller's environment happens
    to already contain (e.g. copied from the stdio setup, which uses
    oauth by default).
    """
    os.environ["MCP_AUTH_MODE"] = "token"
    os.environ.setdefault("MCP_PUBLIC_TOKEN", "unused-fastmcp-token-mode-placeholder")


def build_inner_app() -> Any:
    """Build the bare (unauthenticated-at-our-layer) SSE Starlette app
    from the existing examples/mcp_server FastMCP instance. Caller is
    responsible for wrapping it with BearerAuthMiddleware before serving.

    Always (re)imports examples.mcp_server.server fresh after forcing
    MCP_AUTH_MODE=token, rather than trusting a prior import/cache — a
    module already imported under MCP_AUTH_MODE=oauth (the default) would
    otherwise keep FastMCP's own OAuth auth wired despite this function's
    env override, since Python does not re-run module-level code on a
    plain `import` of an already-imported module.
    """
    _ensure_import_paths()
    _force_fastmcp_auth_unwired()

    import importlib

    module_name = "examples.mcp_server.server"
    if module_name in sys.modules:
        module = importlib.reload(sys.modules[module_name])
    else:
        module = importlib.import_module(module_name)
    return module.mcp.sse_app()


def build_app() -> tuple[Any, str, int]:
    """Validate the environment and return (asgi_app, host, port).

    Order matters: safe mode and token checks happen before the
    (heavier) tool-set import, so a misconfigured environment fails
    fast without ever constructing gateway clients or tool registrations.
    """
    require_safe_mode()
    token = require_bearer_token()
    host = resolve_host()
    port = resolve_port()
    validate_bind_host(host, is_non_loopback_allowed())

    inner_app = build_inner_app()
    app = BearerAuthMiddleware(inner_app, token)
    return app, host, port


def main() -> int:
    try:
        app, host, port = build_app()
    except ConfigError as exc:
        print(f"mcp_sse_serve: refusing to start: {exc}", file=sys.stderr)
        return 1

    import uvicorn  # noqa: PLC0415

    print(f"mcp_sse_serve: starting on {host}:{port} (bearer auth enabled)", file=sys.stderr)
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
