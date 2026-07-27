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
import io
import json
import logging
import re
import socket
import sys
import threading
import time
import warnings
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

# The original 10s startup deadline was found too tight on the shared
# Gitea runner pool under concurrent job load (consistently fine on
# GitHub's isolated VM and locally — timing out only there). 30s gives
# real headroom without masking a genuinely dead server, which is
# still detected immediately via thread liveness, not by waiting out
# the full deadline.
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0


def _redact(s: str) -> str:
    return _SECRET_LIKE.sub("<REDACTED>", s)


class _ThreadResult:
    """Captures an exception raised inside the server's background
    thread, since a plain `threading.Thread` swallows it otherwise.
    """

    def __init__(self) -> None:
        self.exception: BaseException | None = None


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


def _wait_for_started(
    server: Any,
    thread: threading.Thread,
    result: _ThreadResult,
    log_buffer: io.StringIO,
    timeout: float,
) -> None:
    """Poll `server.started` with exponential backoff (not a tight
    busy-loop) until it goes True, the thread dies, or `timeout`
    elapses.

    A dead thread raises immediately — there is no reason to wait out
    the rest of the deadline once the server process is gone. Both
    failure messages include the captured startup exception (if any)
    and any warning/error-level uvicorn log output, redacted, so a CI
    failure is diagnosable without a second run.
    """
    deadline = time.monotonic() + timeout
    delay = 0.02
    max_delay = 0.5
    while time.monotonic() < deadline:
        if server.started:
            return
        if not thread.is_alive():
            raise RuntimeError(
                "ephemeral Streamable HTTP server thread exited before starting"
                f" — captured exception: {_redact(repr(result.exception))};"
                f" log output: {_redact(log_buffer.getvalue())}"
            )
        time.sleep(delay)
        delay = min(delay * 1.5, max_delay)

    raise RuntimeError(
        f"ephemeral Streamable HTTP server did not start within {timeout}s"
        f" — thread alive: {thread.is_alive()};"
        f" log output: {_redact(log_buffer.getvalue())}"
    )


def _run_server_suppressing_deprecation_warnings(server: Any) -> None:
    """Run `server.run()` with DeprecationWarning promotion to a fatal
    exception scoped out, for the duration of this call only.

    Root cause (Phase 18B PR1 Gitea CI blocker): some installed
    uvicorn[standard]/websockets version combinations emit a
    DeprecationWarning (e.g. "websockets.legacy is deprecated") purely
    from uvicorn's optional websocket protocol support initializing —
    even for a plain-HTTP app that never opens a websocket connection.
    Under a strict ambient warning filter (this repo's own pytest
    config uses `filterwarnings=["error"]`), that warning is promoted
    to a real exception at the exact point it's raised, which — before
    this fix — was indistinguishable from a genuine startup crash to
    the caller. Not reproducible with the locally pinned
    uvicorn/websockets versions; observed only on the shared Gitea CI
    runner, which is why this is a code-level fix rather than a
    version pin.

    `warnings.catch_warnings()` is not thread-safe against *other*
    threads concurrently mutating the global filter list, but nothing
    else in this script's own code does so during a probe run.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        server.run()


def run_ephemeral_server(
    app: Any,
    host: str,
    port: int,
    *,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
) -> tuple[Any, threading.Thread]:
    """Start uvicorn in a background thread for the probe's duration
    only. Caller must call stop_ephemeral_server() before exit.

    `startup_timeout` is a keyword-only parameter (not hardcoded)
    specifically so a slower CI runner can be given more headroom, and
    so tests can exercise the failure path without waiting out the
    real default.
    """
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    log_buffer = io.StringIO()
    log_handler = logging.StreamHandler(log_buffer)
    log_handler.setLevel(logging.WARNING)
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.addHandler(log_handler)

    result = _ThreadResult()

    def _run() -> None:
        try:
            _run_server_suppressing_deprecation_warnings(server)
        except BaseException as exc:  # noqa: BLE001 - must capture, thread swallows otherwise
            result.exception = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    try:
        _wait_for_started(server, thread, result, log_buffer, startup_timeout)
    finally:
        uvicorn_logger.removeHandler(log_handler)

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


def run_probe(*, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Build the app, discover routes/methods, run the ephemeral
    server, fire the probes, tear the server down. Returns a single
    evidence dict — used both by __main__ and by the contract test.
    """
    app = build_streamable_http_app()

    routes = discover_routes(app)
    methods = discover_methods(app)

    host = "127.0.0.1"
    port = find_free_port()
    server, thread = run_ephemeral_server(app, host, port, startup_timeout=startup_timeout)
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
