#!/usr/bin/env python3
"""Streamable HTTP MCP route discovery spike (Phase 18B PR1).

Purpose: replace the Phase 18A design doc's source-inspection-only
claims about `FastMCP.streamable_http_app()` (docs/superpowers/specs/
2026-07-26-private-streamable-http-mcp-transport.md, sections 4 and 9)
with empirical evidence — real routes, real methods, real HTTP status
codes, real response headers — gathered by actually building the app
and firing real requests at it, not by reading SDK source alone.

This is a one-shot discovery script, not an entrypoint:

  - No bearer-token middleware, no Origin-validation middleware are
    added here. The app is served bare. Auth/Origin wiring is PR2's
    job (scripts/mcp_streamable_http_serve.py), which will reuse
    BearerAuthMiddleware/OriginValidationMiddleware from
    mcp_sse_serve.py exactly as this script's findings dictate.
  - The HTTP server it starts is ephemeral: bound to 127.0.0.1 on a
    freshly-allocated free port, for the duration of this process
    only, torn down before exit. Nothing is left listening, nothing
    is added to Docker Compose or systemd.
  - No gateway credential (GATEWAY_URL / GATEWAY_API_KEY /
    GATEWAY_AGENT_TOKEN) is required or read. Building the FastMCP
    instance and its Streamable HTTP app does not need a live
    gateway — only the `health` tool's own call would, and this
    script never calls any tool.
  - Requires the same safe-mode preconditions as every other MCP
    entrypoint in this repo (MCP_GATEWAY_TOOL_MODE=chatgpt,
    MCP_CHATGPT_SAFE_MODE=true), reusing require_safe_mode() from
    mcp_sse_serve.py rather than re-implementing the check.

Run directly:
    MCP_GATEWAY_TOOL_MODE=chatgpt MCP_CHATGPT_SAFE_MODE=true \
        python3 scripts/mcp_streamable_http_route_probe.py

All findings are printed to stderr as JSON (routes, methods, and the
raw probe results for GET/POST/DELETE /mcp) — this printed output is
the evidence artifact for PR2/PR3 to build against, not an assumption.
"""

from __future__ import annotations

import importlib
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER_DIR = ROOT / "examples" / "mcp_server"

for _p in (str(MCP_SERVER_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.mcp_sse_serve import (  # noqa: E402
    ConfigError,
    _force_fastmcp_auth_unwired,
    discover_routes,
    require_safe_mode,
)

_SECRET_LIKE = re.compile(r"[A-Za-z0-9_\-]{20,}")


def _redact(s: str) -> str:
    return _SECRET_LIKE.sub("<REDACTED>", s)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_streamable_http_app() -> Any:
    """Build a bare Streamable HTTP Starlette app — no auth, no Origin
    validation. Mirrors mcp_sse_serve.py's build_inner_app() import/
    reload pattern, swapping .sse_app() for .streamable_http_app().

    Always (re)imports examples.mcp_server.server fresh after forcing
    MCP_AUTH_MODE=token, for the same reason build_inner_app() does:
    a module already imported under the default MCP_AUTH_MODE=oauth
    would keep FastMCP's own OAuth auth wired despite this function's
    env override, since a plain `import` does not re-run module-level
    code for an already-imported module.
    """
    require_safe_mode()
    _force_fastmcp_auth_unwired()

    module_name = "examples.mcp_server.server"
    if module_name in sys.modules:
        module = importlib.reload(sys.modules[module_name])
    else:
        module = importlib.import_module(module_name)
    return module.mcp.streamable_http_app()


def discover_methods(app: Any) -> list[dict[str, Any]]:
    """Return path + accepted-methods info read directly off the
    Starlette route objects — not assumed from the spec's prose.
    """
    found = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        found.append(
            {
                "route_class": type(route).__name__,
                "path": getattr(route, "path", None),
                "methods": sorted(methods) if methods else None,
            }
        )
    return found


def run_ephemeral_server(app: Any, host: str, port: int) -> tuple[Any, threading.Thread]:
    """Start uvicorn in a background thread for the probe's duration
    only. Caller must call stop_ephemeral_server() before exit.
    """
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("ephemeral Streamable HTTP server did not start in time")

    return server, thread


def stop_ephemeral_server(server: Any, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10.0)


def probe(base_url: str) -> dict[str, Any]:
    """Fire the three real HTTP probes required by this PR and return
    the actual observed status/headers — never assumed values.
    """
    import httpx

    results: dict[str, Any] = {}
    accept = "application/json, text/event-stream"

    with httpx.Client(timeout=5.0) as client:
        try:
            r = client.get(f"{base_url}/mcp", headers={"Accept": accept})
            results["GET /mcp"] = {
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "mcp_session_id": r.headers.get("mcp-session-id"),
            }
        except httpx.HTTPError as exc:
            results["GET /mcp"] = {"error": str(exc)}

        try:
            r = client.post(
                f"{base_url}/mcp",
                json={"not": "a-valid-jsonrpc-request"},
                headers={"Accept": accept, "Content-Type": "application/json"},
            )
            results["POST /mcp (malformed body)"] = {
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "mcp_session_id": r.headers.get("mcp-session-id"),
            }
        except httpx.HTTPError as exc:
            results["POST /mcp (malformed body)"] = {"error": str(exc)}

        try:
            r = client.request("DELETE", f"{base_url}/mcp", headers={"Accept": accept})
            results["DELETE /mcp (no session header)"] = {
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
            }
        except httpx.HTTPError as exc:
            results["DELETE /mcp (no session header)"] = {"error": str(exc)}

    return results


def run_probe() -> dict[str, Any]:
    """Build the app, discover routes/methods, run the ephemeral
    server, fire the probes, tear the server down. Returns a single
    evidence dict — used both by __main__ and by the contract test.
    """
    app = build_streamable_http_app()

    routes = discover_routes(app)
    methods = discover_methods(app)

    host = "127.0.0.1"
    port = find_free_port()
    server, thread = run_ephemeral_server(app, host, port)
    try:
        probe_results = probe(f"http://{host}:{port}")
    finally:
        stop_ephemeral_server(server, thread)

    return {
        "routes": routes,
        "methods": methods,
        "probe_results": probe_results,
    }


def main() -> int:
    try:
        evidence = run_probe()
    except ConfigError as exc:
        print(f"mcp_streamable_http_route_probe: refusing to start: {exc}", file=sys.stderr)
        return 1

    print("=== Streamable HTTP route discovery (empirical, PR1) ===", file=sys.stderr)
    print(_redact(json.dumps(evidence, indent=2)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
