#!/usr/bin/env python3
"""Streamable HTTP runtime smoke test for
scripts/mcp_streamable_http_serve.py (Phase 18B PR3).

Starts the private Streamable HTTP MCP entrypoint as a real subprocess
bound to 127.0.0.1 on a free local port, then checks:

  - POST /mcp without token                          -> 401
  - POST /mcp with wrong token                       -> 401
  - POST /mcp with valid token + non-loopback Origin  -> 403
  - valid token + no Origin: MCP initialize, list_tools, tools_manifest
   - safe tool count == 75, blocked tool count == 29, blocked tools absent
  - whether a Mcp-Session-Id was assigned (reported as present/absent
    only — the value itself is never printed)

Route is /mcp (not /sse or /messages) — the empirically-discovered
Phase 18B PR1 route, reused unchanged by PR2's entrypoint and by this
smoke test.

Env var note: the entrypoint's non-loopback override is
MCP_STREAMABLE_HTTP_ALLOW_NON_LOOPBACK — this script never sets it,
since it only ever runs the server on 127.0.0.1.

Session behavior (reported honestly, not assumed): this entrypoint
runs the mcp SDK's session manager in its default (non-stateless)
mode, so a fresh Mcp-Session-Id is assigned at `initialize` and must
be echoed by the client on subsequent requests — the mcp Python
client library (streamable_http_client) handles this automatically.
This smoke test does not exercise session *resumption* (reconnecting
with Last-Event-ID) or multi-client session isolation — both are
out of scope for this phase, same as the Phase 18B design doc's own
stated non-goals.

Exit codes:
  0 — all mandatory checks passed (protocol-level checks are best-effort
      and reported separately; see below)
  1 — a mandatory check failed (auth bypassed, safe mode not enforced,
      blocked tool reachable, server never became reachable)

Never prints MCP_STREAMABLE_HTTP_BEARER_TOKEN, GATEWAY_API_KEY,
GATEWAY_AGENT_TOKEN, or any Mcp-Session-Id value. Always terminates
the subprocess.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER_DIR = ROOT / "examples" / "mcp_server"
SERVER_ENTRYPOINT = ROOT / "scripts" / "mcp_streamable_http_serve.py"

for _p in (str(MCP_SERVER_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PASS = 0
FAIL = 0


def _redact(s: str) -> str:
    return re.sub(r"[A-Za-z0-9_\-]{20,}", "<REDACTED>", s)


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "✅" if ok else "❌"
    suffix = f" — {detail}" if detail else ""
    print(f"  {status} {label}{suffix}")
    if ok:
        PASS += 1
    else:
        FAIL += 1


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def generate_test_token() -> str:
    return secrets.token_urlsafe(32)


def build_env(host: str, port: int, token: str) -> dict[str, str]:
    """Isolated env dict for the subprocess — never mutates os.environ.

    Gateway env vars are passed through only if already present in the
    parent process; the entrypoint and tool registration work fine
    without a live gateway (tool_modes filtering and tools_manifest are
    local-only). Only the `health` tool's gateway call would fail
    without them, and that is treated as non-fatal below, mirroring
    scripts/mcp_sse_safe_smoke.py's convention.
    """
    env: dict[str, str] = {
        "MCP_STREAMABLE_HTTP_HOST": host,
        "MCP_STREAMABLE_HTTP_PORT": str(port),
        "MCP_STREAMABLE_HTTP_BEARER_TOKEN": token,
        "MCP_GATEWAY_TOOL_MODE": "mcp_client",
        "MCP_CLIENT_SAFE_MODE": "true",
        "MCP_ACCESS_PROFILE": "mcp_client_safe",
    }

    for key in ("GATEWAY_URL", "GATEWAY_AGENT_TOKEN"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if env.get("GATEWAY_AGENT_TOKEN"):
        env.setdefault("GATEWAY_API_KEY", env["GATEWAY_AGENT_TOKEN"])

    python = sys.executable
    env["PATH"] = f"{Path(python).parent}:{os.environ.get('PATH', '')}"
    env["PYTHONPATH"] = str(ROOT)
    env.pop("VIRTUAL_ENV", None)
    return env


def wait_for_server(host: str, port: int, timeout: float = 10.0) -> bool:
    """Poll a raw TCP connect until the server accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def start_server(env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(SERVER_ENTRYPOINT)],
        env=env,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def check_http_auth_and_origin(base_url: str, token: str, transport: Any | None = None) -> None:
    """Route + auth + Origin checks via plain HTTP POST — no MCP
    protocol involved yet.

    `transport` is exposed only so tests can inject an in-memory
    httpx.ASGITransport (no real socket) to prove this function
    targets /mcp — never a guessed path like /sse or /messages —
    without requiring a real subprocess/network. Production calls
    (the real smoke run) never pass it, so httpx uses a real TCP
    connection.
    """
    import httpx

    accept = "application/json, text/event-stream"
    with httpx.Client(timeout=5.0, transport=transport) as client:
        resp = client.post(f"{base_url}/mcp", json={}, headers={"Accept": accept})
        check("POST /mcp no token -> 401", resp.status_code == 401, f"got {resp.status_code}")

        resp = client.post(
            f"{base_url}/mcp",
            json={},
            headers={"Accept": accept, "Authorization": "Bearer wrong-token-value"},
        )
        check("POST /mcp wrong token -> 401", resp.status_code == 401, f"got {resp.status_code}")

        resp = client.post(
            f"{base_url}/mcp",
            json={},
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {token}",
                "Origin": "http://evil.example.com",
            },
        )
        check(
            "POST /mcp valid token + non-loopback Origin -> 403",
            resp.status_code == 403,
            f"got {resp.status_code}",
        )


async def run_protocol_checks(base_url: str, token: str) -> None:
    """MCP protocol over Streamable HTTP: initialize, list_tools,
    tools_manifest. Sends no Origin header (matching a CLI/local
    client) — only the auth/Origin checks above exercise the Origin
    guard.

    Best-effort: network/timing issues here are reported as failed
    checks (not silently skipped, not faked as success), but do not
    abort the auth/route checks that already ran above.
    """
    from tool_modes import MCP_CLIENT_BLOCKED_TOOLS, get_mcp_client_safe_tools

    try:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        check("MCP protocol over Streamable HTTP", False, f"mcp client unavailable: {exc}")
        return

    expected_safe = get_mcp_client_safe_tools()
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=10.0
        ) as http_client:
            async with (
                streamable_http_client(f"{base_url}/mcp", http_client=http_client) as (
                    read,
                    write,
                    get_session_id,
                ),
                ClientSession(read, write) as session,
            ):
                init_result = await session.initialize()
                check(
                    "MCP initialize over Streamable HTTP",
                    bool(init_result.protocolVersion),
                    f"protocolVersion={init_result.protocolVersion}",
                )

                # Session behavior is reported honestly — present/absent
                # only, never the value itself (it is a credential-
                # adjacent, unique-per-session identifier).
                session_id = get_session_id()
                check(
                    "Mcp-Session-Id assigned after initialize",
                    bool(session_id),
                    "present" if session_id else "absent",
                )

                tools_result = await session.list_tools()
                tool_names = {t.name for t in tools_result.tools}
                check("list_tools count == 75", len(tool_names) == 75, f"got {len(tool_names)}")
                check(
                    "blocked tool count == 29",
                    len(MCP_CLIENT_BLOCKED_TOOLS) == 29,
                    f"got {len(MCP_CLIENT_BLOCKED_TOOLS)}",
                )

                overlap = tool_names & MCP_CLIENT_BLOCKED_TOOLS
                check(
                    "blocked tools absent from live manifest",
                    not overlap,
                    f"leaked={sorted(overlap)}",
                )
                check(
                    "safe tools match tool_modes.get_mcp_client_safe_tools()",
                    tool_names == expected_safe,
                )

                manifest_result = await session.call_tool("tools_manifest", {})
                is_error = getattr(manifest_result, "isError", False)
                check("tools_manifest call over Streamable HTTP", not is_error)

                health_result = await session.call_tool("health", {})
                health_is_error = getattr(health_result, "isError", False)
                if health_is_error:
                    print(
                        "  (health tool call failed — non-fatal, likely no live "
                        "gateway configured)"
                    )
    except Exception as exc:  # noqa: BLE001 — protocol-level, report don't crash
        check("MCP protocol over Streamable HTTP", False, _redact(str(exc)[:200]))


def main() -> int:
    print("=== MCP Streamable HTTP Safe Smoke ===\n")

    host = "127.0.0.1"
    port = find_free_port()
    token = generate_test_token()
    base_url = f"http://{host}:{port}"

    env = build_env(host, port, token)
    proc = start_server(env)
    try:
        reachable = wait_for_server(host, port, timeout=10.0)
        check("server reachable", reachable, f"{host}:{port}")
        if not reachable:
            out, err = proc.communicate(timeout=2) if proc.poll() is not None else ("", "")
            print(f"  subprocess stdout: {_redact(out[-500:])}")
            print(f"  subprocess stderr: {_redact(err[-500:])}")
            return 1

        check_http_auth_and_origin(base_url, token)

        import asyncio

        asyncio.run(run_protocol_checks(base_url, token))
    finally:
        stop_server(proc)

    print(f"\n{'=' * 40}\nResults: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
