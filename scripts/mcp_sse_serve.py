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
  MCP_HTTP_ALLOWED_ORIGINS     optional comma-separated extra Origin allowlist
  MCP_CLIENT_SAFE_MODE        must be "true"
  MCP_GATEWAY_TOOL_MODE        must be "mcp_client"

MCP_HTTP_BEARER_TOKEN is never logged or echoed back in responses.

Origin validation (Phase 17B): per the MCP transport spec's DNS-rebinding
guard ("Servers MUST validate the Origin header on all incoming
connections"), every request to /sse and /messages is also checked
against an Origin allowlist, ahead of the bearer check. A missing
Origin header is allowed (CLI/curl/local MCP clients routinely omit
it). Loopback Origins (http(s)://127.0.0.1:*, http(s)://localhost:*,
http(s)://[::1]:*) are allowed by default. MCP_HTTP_ALLOWED_ORIGINS can
add exact extra origins (e.g. a specific local dev tool's origin) —
it is not a path to public exposure; non-loopback origins not in that
explicit list are rejected with 403 regardless of bearer token
validity. Only the sanitized Origin *host* is ever logged on rejection,
never the full header value.
"""

from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def resolve_host(env_var: str = "MCP_HTTP_HOST") -> str:
    return os.environ.get(env_var, DEFAULT_HOST).strip() or DEFAULT_HOST


def resolve_port(env_var: str = "MCP_HTTP_PORT", default: int = DEFAULT_PORT) -> int:
    """`env_var`/`default` let a sibling entrypoint (e.g. Streamable
    HTTP) reuse this function with its own env var name and default
    port, without touching SSE's own default call (env_var/default
    keep SSE's exact existing behavior when omitted).
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{env_var} must be an integer, got {raw!r}") from exc


def validate_bind_host(
    host: str,
    allow_non_loopback: bool,
    *,
    override_env_var: str = "MCP_HTTP_ALLOW_NON_LOOPBACK",
    transport_label: str = "MCP SSE",
) -> None:
    """Fail fast unless host is loopback or the override is explicitly
    set. `override_env_var`/`transport_label` let a sibling entrypoint
    reuse this with its own env var name in the error message, without
    changing SSE's own default call.
    """
    if host in LOOPBACK_HOSTS:
        return
    if allow_non_loopback:
        return
    raise ConfigError(
        f"Refusing to bind {transport_label} server to non-loopback host {host!r}. "
        f"Set {override_env_var}=true to override explicitly "
        "(not recommended outside a reviewed private-network deployment)."
    )


def is_non_loopback_allowed(env_var: str = "MCP_HTTP_ALLOW_NON_LOOPBACK") -> bool:
    return os.environ.get(env_var, "").strip().lower() == "true"


_SAFE_HTTP_ENTRYPOINT_MODES = ("mcp_client", "mcp_client_write")


def require_safe_mode() -> None:
    """Fail fast unless the server is configured for a known-safe ChatGPT mode.

    Reuses examples.mcp_server.tool_modes as the single source of truth
    instead of re-implementing the env var parsing here.

    Two modes are accepted:
      - "mcp_client": fully read-only/safe (requires MCP_CLIENT_SAFE_MODE=true).
      - "mcp_client_write": adds project file write/edit/patch and git
        add/commit/push, but still excludes Docker admin, agent-launch,
        and handoff/agent-task-plan writes (MCP_CLIENT_WRITE_BLOCKED_TOOLS
        in tool_modes.py) -- a deliberate, narrower opt-in, not a relaxation
        of "mcp_client" itself.

    Any other mode (standard/full/minimal) exposes Docker/Postgres/agent-
    launch tools with no restriction at all and must never reach either
    private HTTP entrypoint.
    """
    _ensure_import_paths()
    from tool_modes import get_tool_mode, is_mcp_client_safe_mode  # noqa: PLC0415

    mode = get_tool_mode()
    if mode not in _SAFE_HTTP_ENTRYPOINT_MODES:
        allowed = " or ".join(repr(m) for m in _SAFE_HTTP_ENTRYPOINT_MODES)
        raise ConfigError(
            f"MCP_GATEWAY_TOOL_MODE must be {allowed} for the private SSE "
            f"entrypoint, got {mode!r}."
        )
    if mode == "mcp_client" and not is_mcp_client_safe_mode():
        raise ConfigError(
            "MCP_CLIENT_SAFE_MODE must be true for the private SSE entrypoint "
            "when MCP_GATEWAY_TOOL_MODE=mcp_client."
        )


def require_bearer_token(env_var: str = "MCP_HTTP_BEARER_TOKEN") -> str:
    token = os.environ.get(env_var, "")
    if not token:
        raise ConfigError(f"{env_var} is required to start this entrypoint.")
    return token


def parse_allowed_origins(env_var: str = "MCP_HTTP_ALLOWED_ORIGINS") -> frozenset[str]:
    """Extra exact-match Origin allowlist from `env_var` (comma-
    separated). Empty by default — loopback origins are already
    allowed unconditionally by is_origin_allowed() below, so this is
    only for adding specific non-default-loopback origins, not a
    general public-exposure mechanism.
    """
    raw = os.environ.get(env_var, "")
    return frozenset(o.strip() for o in raw.split(",") if o.strip())


def _origin_host(origin: str) -> str | None:
    try:
        return urlparse(origin).hostname
    except ValueError:
        return None


def is_origin_allowed(origin: str | None, extra_allowed: frozenset[str]) -> bool:
    """Per the MCP transport spec's DNS-rebinding guard: validate Origin.

    - No Origin header at all -> allowed (CLI/curl/local MCP clients
      routinely omit it; this is not a browser request).
    - Origin present and loopback (127.0.0.1 / localhost / ::1, any
      scheme, any port) -> allowed by default.
    - Origin present and exactly matches an entry in extra_allowed
      (MCP_HTTP_ALLOWED_ORIGINS) -> allowed.
    - Anything else -> rejected.
    """
    if not origin:
        return True
    if origin in extra_allowed:
        return True
    host = _origin_host(origin)
    return host is not None and host.lower() in LOOPBACK_HOSTS


class OriginValidationMiddleware:
    """Validates the Origin header before any other processing, including
    the bearer check. Wraps the whole app, same pattern as
    BearerAuthMiddleware, so /sse and /messages are both covered by one
    check. Never logs the full Origin value on rejection — only the
    parsed host, which is what matters for the DNS-rebinding threat model.
    """

    def __init__(self, app: Any, extra_allowed_origins: frozenset[str]) -> None:
        self.app = app
        self._extra_allowed = extra_allowed_origins

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_origin = headers.get(b"origin")
        origin = raw_origin.decode("latin-1") if raw_origin is not None else None

        if not is_origin_allowed(origin, self._extra_allowed):
            sanitized_host = _origin_host(origin) if origin else None
            print(
                f"mcp_sse_serve: rejected request — Origin not allowed "
                f"(host={sanitized_host!r})",
                file=sys.stderr,
            )
            from starlette.responses import JSONResponse  # noqa: PLC0415

            response = JSONResponse({"error": "origin_not_allowed"}, status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class BearerAuthMiddleware:
    """Minimal ASGI middleware requiring `Authorization: Bearer <token>`.

    Wraps the whole app (not a per-route Starlette dependency), so both
    the SSE GET endpoint and the message POST endpoint are protected by
    the same check before any MCP routing happens. Independent of
    MCP_AUTH_MODE / GatewayOAuthProvider — does not assume that wiring
    is correct.

    exempt_paths (default: none) lets a caller carve out unauthenticated
    liveness-probe paths — e.g. a Docker HEALTHCHECK that would otherwise
    have to hit the real (bearer-protected) MCP endpoint and log a 401 on
    every single check. An exempt path never reaches the wrapped app; it
    always gets a fixed, minimal 200 response, so it can't become a way
    to smuggle real traffic past auth by picking a path that happens to
    exist in the wrapped app too.
    """

    def __init__(self, app: Any, token: str, *, exempt_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self._token = token
        self._exempt_paths = exempt_paths

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") in self._exempt_paths:
            from starlette.responses import JSONResponse  # noqa: PLC0415

            response = JSONResponse({"status": "ok"})
            await response(scope, receive, send)
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


def _extend_sdk_transport_security(mcp_instance: Any, extra_origins: frozenset[str]) -> None:
    """Extend the mcp SDK's own built-in Origin/Host allowlist
    (mcp.server.transport_security.TransportSecuritySettings) with
    MCP_HTTP_ALLOWED_ORIGINS entries.

    FastMCP already ships a DNS-rebinding guard by default — confirmed by
    inspection: FastMCP(...).settings.transport_security defaults to
    enable_dns_rebinding_protection=True with allowed_origins already
    restricted to loopback wildcards (http://127.0.0.1:*,
    http://localhost:*, http://[::1]:*). This is a second, independent
    layer from this script's own OriginValidationMiddleware — both must
    agree, or a custom origin allowed by one gets rejected by the other
    (found via direct testing: an origin added only to
    OriginValidationMiddleware's allowlist was still rejected deeper in
    mcp.server.sse.connect_sse() with "Invalid Origin header").

    Only appends — never removes the SDK's own sane loopback defaults.
    """
    if not extra_origins:
        return
    settings = mcp_instance.settings.transport_security
    settings.allowed_origins = list(settings.allowed_origins) + list(extra_origins)


def build_inner_app(extra_allowed_origins: frozenset[str] = frozenset()) -> Any:
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
    _extend_sdk_transport_security(module.mcp, extra_allowed_origins)
    return module.mcp.sse_app()


def build_app() -> tuple[Any, str, int]:
    """Validate the environment and return (asgi_app, host, port).

    Order matters: safe mode and token checks happen before the
    (heavier) tool-set import, so a misconfigured environment fails
    fast without ever constructing gateway clients or tool registrations.

    Middleware order matters too: OriginValidationMiddleware wraps
    BearerAuthMiddleware (not the other way around), so an invalid
    Origin is rejected with 403 before the bearer token is even
    inspected — matching the MCP transport spec's framing of Origin
    validation as a connection-level guard, separate from application
    auth.
    """
    require_safe_mode()
    token = require_bearer_token()
    host = resolve_host()
    port = resolve_port()
    validate_bind_host(host, is_non_loopback_allowed())
    extra_allowed_origins = parse_allowed_origins()

    inner_app = build_inner_app(extra_allowed_origins)
    app: Any = BearerAuthMiddleware(inner_app, token)
    app = OriginValidationMiddleware(app, extra_allowed_origins)
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
