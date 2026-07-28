#!/usr/bin/env python3
"""Private Streamable HTTP MCP transport entrypoint (Phase 18B PR2).

Serves the existing examples/mcp_server FastMCP tool set over the MCP
spec's current Streamable HTTP transport (protocol version
2025-06-18), bound to loopback by default — a sibling of
scripts/mcp_sse_serve.py, not a replacement for it. SSE is deprecated
at the protocol-spec level, not removed from this repo; this script
adds a second, additive private transport, exactly as designed in
docs/superpowers/specs/2026-07-26-private-streamable-http-mcp-transport.md
and planned in
docs/superpowers/plans/2026-07-26-private-streamable-http-mcp-implementation.md.

Route, methods, and status-code behavior are the ones empirically
discovered by scripts/mcp_streamable_http_route_probe.py (Phase 18B
PR1): a single `/mcp` endpoint, any HTTP method reaches the SDK's own
session manager, and GET/POST/DELETE without a valid MCP payload or
session all return 400 (not 405) — this entrypoint does not change
that behavior, it only adds bearer auth and Origin validation in front
of it.

Does not modify examples/mcp_server/server.py's stdio path (the
module is imported, not edited). Does not add Docker Compose / systemd
wiring. Does not enable public bind by default —
MCP_STREAMABLE_HTTP_ALLOW_NON_LOOPBACK=true is required to relax the
loopback-only guard, and this script does not set it.

This process always forces MCP_AUTH_MODE=token in its own environment
before importing examples.mcp_server.server, for the same reason
mcp_sse_serve.py does (see build_streamable_http_app() in
mcp_streamable_http_route_probe.py, reused here unchanged): without
it, the default MCP_AUTH_MODE=oauth wires a real FastMCP-native auth
layer that rejects MCP_STREAMABLE_HTTP_BEARER_TOKEN with its own error
response, unrelated to this script's own BearerAuthMiddleware.

Env vars:
  MCP_STREAMABLE_HTTP_HOST                default 127.0.0.1
  MCP_STREAMABLE_HTTP_PORT                default 8087 (distinct from
                                           SSE's 8086 — both entrypoints
                                           can run at once)
  MCP_STREAMABLE_HTTP_ALLOW_NON_LOOPBACK  must be exactly "true" to
                                           bind non-loopback
  MCP_STREAMABLE_HTTP_BEARER_TOKEN        required — protects /mcp
  MCP_STREAMABLE_HTTP_ALLOWED_ORIGINS     optional comma-separated
                                           extra Origin allowlist
  MCP_CLIENT_SAFE_MODE                   must be "true"
  MCP_GATEWAY_TOOL_MODE                   must be "mcp_client"

MCP_STREAMABLE_HTTP_BEARER_TOKEN is never logged or echoed back in
responses.

Origin validation: per the MCP transport spec's DNS-rebinding guard
("Servers MUST validate the Origin header on all incoming
connections"), every request to /mcp is checked against an Origin
allowlist ahead of the bearer check — identical policy to
mcp_sse_serve.py's OriginValidationMiddleware (reused, not
reimplemented): a missing Origin header is allowed (CLI/curl/local MCP
clients routinely omit it); loopback Origins are allowed by default;
MCP_STREAMABLE_HTTP_ALLOWED_ORIGINS can add exact extra origins — it
is not a path to public exposure; anything else is rejected with 403
regardless of bearer token validity. Only the sanitized Origin *host*
is ever logged on rejection, never the full header value.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER_DIR = ROOT / "examples" / "mcp_server"

for _p in (str(MCP_SERVER_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.mcp_sse_serve import (  # noqa: E402
    BearerAuthMiddleware,
    ConfigError,
    OriginValidationMiddleware,
    require_safe_mode,
    validate_bind_host,
)
from scripts.mcp_sse_serve import is_non_loopback_allowed as _is_non_loopback_allowed  # noqa: E402
from scripts.mcp_sse_serve import parse_allowed_origins as _parse_allowed_origins  # noqa: E402
from scripts.mcp_sse_serve import require_bearer_token as _require_bearer_token  # noqa: E402
from scripts.mcp_sse_serve import resolve_host as _resolve_host  # noqa: E402
from scripts.mcp_sse_serve import resolve_port as _resolve_port  # noqa: E402
from scripts.mcp_streamable_http_route_probe import (  # noqa: E402
    build_streamable_http_app as _build_streamable_http_app,
)

HOST_ENV_VAR = "MCP_STREAMABLE_HTTP_HOST"
PORT_ENV_VAR = "MCP_STREAMABLE_HTTP_PORT"
DEFAULT_PORT = 8087
ALLOW_NON_LOOPBACK_ENV_VAR = "MCP_STREAMABLE_HTTP_ALLOW_NON_LOOPBACK"
BEARER_TOKEN_ENV_VAR = "MCP_STREAMABLE_HTTP_BEARER_TOKEN"
ALLOWED_ORIGINS_ENV_VAR = "MCP_STREAMABLE_HTTP_ALLOWED_ORIGINS"
TRANSPORT_LABEL = "MCP Streamable HTTP"


def resolve_host() -> str:
    return _resolve_host(HOST_ENV_VAR)


def resolve_port() -> int:
    return _resolve_port(PORT_ENV_VAR, default=DEFAULT_PORT)


def is_non_loopback_allowed() -> bool:
    return _is_non_loopback_allowed(ALLOW_NON_LOOPBACK_ENV_VAR)


def require_bearer_token() -> str:
    return _require_bearer_token(BEARER_TOKEN_ENV_VAR)


def parse_allowed_origins() -> frozenset[str]:
    return _parse_allowed_origins(ALLOWED_ORIGINS_ENV_VAR)


def build_inner_app(extra_allowed_origins: frozenset[str] = frozenset()) -> Any:
    """Build the bare (unauthenticated-at-our-layer) Streamable HTTP
    Starlette app. Thin wrapper over
    mcp_streamable_http_route_probe.build_streamable_http_app() —
    reused, not duplicated, so the exact same safe-mode/auth-unwiring/
    transport-security-extension logic backs both PR1's probe and this
    entrypoint.
    """
    return _build_streamable_http_app(extra_allowed_origins)


def build_app() -> tuple[Any, str, int]:
    """Validate the environment and return (asgi_app, host, port).

    Mirrors mcp_sse_serve.py's build_app() exactly: safe mode and
    token checks before the heavier tool-set import, and
    OriginValidationMiddleware wrapping BearerAuthMiddleware (not the
    other way around) so an invalid Origin is rejected with 403 before
    the bearer token is even inspected.
    """
    require_safe_mode()
    token = require_bearer_token()
    host = resolve_host()
    port = resolve_port()
    validate_bind_host(
        host,
        is_non_loopback_allowed(),
        override_env_var=ALLOW_NON_LOOPBACK_ENV_VAR,
        transport_label=TRANSPORT_LABEL,
    )
    extra_allowed_origins = parse_allowed_origins()

    inner_app = build_inner_app(extra_allowed_origins)
    app: Any = BearerAuthMiddleware(inner_app, token)
    app = OriginValidationMiddleware(app, extra_allowed_origins)
    return app, host, port


def main() -> int:
    try:
        app, host, port = build_app()
    except ConfigError as exc:
        print(f"mcp_streamable_http_serve: refusing to start: {exc}", file=sys.stderr)
        return 1

    import uvicorn  # noqa: PLC0415

    print(
        f"mcp_streamable_http_serve: starting on {host}:{port} (bearer auth enabled)",
        file=sys.stderr,
    )
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
