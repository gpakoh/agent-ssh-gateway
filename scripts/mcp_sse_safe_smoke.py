#!/usr/bin/env python3
"""SSE runtime smoke test for scripts/mcp_sse_serve.py (Phase 16B PR2).

Starts the private SSE MCP entrypoint as a real subprocess bound to
127.0.0.1 on a free local port, then checks:

  - GET /sse without token            -> 401
  - GET /sse with wrong token         -> 401
  - GET /sse with correct token       -> non-401 (SSE stream opens)
  - POST /messages/ without token    -> 401
  - MCP protocol over SSE: initialize, list_tools, tools_manifest
  - safe tool count == 84, blocked tool count == 30, blocked tools absent

Routes are /sse and /messages (not /mcp/sse) — see the Phase 16A plan's
documented mismatch between the original spec pseudocode and the real
FastMCP route registration (confirmed empirically in PR1). This script
uses the real, verified paths.

Env var note: the entrypoint's non-loopback override is
MCP_HTTP_ALLOW_NON_LOOPBACK (not the older MCP_HTTP_BIND_PUBLIC name
used in the original design docs) — this script never sets it, since
it only ever runs the server on 127.0.0.1.

Exit codes:
  0 — all mandatory checks passed (protocol-level checks are best-effort
      and reported separately; see below)
  1 — a mandatory check failed (auth bypassed, safe mode not enforced,
      blocked tool reachable, server never became reachable)

Never prints MCP_HTTP_BEARER_TOKEN, GATEWAY_API_KEY, or
GATEWAY_AGENT_TOKEN. Always terminates the subprocess.
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
SERVER_ENTRYPOINT = ROOT / "scripts" / "mcp_sse_serve.py"

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
    scripts/mcp_stdio_safe_smoke.py's convention.
    """
    env: dict[str, str] = {
        "MCP_HTTP_HOST": host,
        "MCP_HTTP_PORT": str(port),
        "MCP_HTTP_BEARER_TOKEN": token,
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


def check_http_auth(base_url: str, token: str, transport: Any | None = None) -> None:
    """Route + auth checks via plain HTTP — no MCP protocol involved.

    `transport` is exposed only so tests can inject an in-memory
    httpx.ASGITransport (no real socket) to prove this function targets
    /sse and /messages — never a guessed path like /mcp/sse — without
    requiring a real subprocess/network. Production calls (the real
    smoke run) never pass it, so httpx uses a real TCP connection.
    """
    import httpx

    with httpx.Client(timeout=5.0, transport=transport) as client:
        resp = client.get(f"{base_url}/sse")
        check("GET /sse no token -> 401", resp.status_code == 401, f"got {resp.status_code}")

        resp = client.get(f"{base_url}/sse", headers={"Authorization": "Bearer wrong-token-value"})
        check("GET /sse wrong token -> 401", resp.status_code == 401, f"got {resp.status_code}")

        resp = client.post(f"{base_url}/messages/", json={})
        check("POST /messages/ no token -> 401", resp.status_code == 401, f"got {resp.status_code}")

    # SSE never completes its body — must use streaming mode and only
    # inspect the status line, not consume the stream.
    try:
        with httpx.Client(timeout=5.0, transport=transport) as client, client.stream(
            "GET", f"{base_url}/sse", headers={"Authorization": f"Bearer {token}"}
        ) as resp:
            check(
                "GET /sse correct token -> non-401 (stream opens)",
                resp.status_code != 401,
                f"got {resp.status_code}",
            )
    except httpx.ReadTimeout:
        check("GET /sse correct token -> non-401 (stream opens)", False, "read timeout")


async def run_protocol_checks(base_url: str, token: str) -> None:
    """MCP protocol over SSE: initialize, list_tools, tools_manifest.

    Best-effort: network/timing issues here are reported as failed
    checks (not silently skipped, not faked as success), but do not
    abort the auth/route checks that already ran above.
    """
    from tool_modes import MCP_CLIENT_BLOCKED_TOOLS, get_mcp_client_safe_tools

    try:
        from mcp import ClientSession
        from mcp.client.sse import sse_client
    except ImportError as exc:
        check("MCP protocol over SSE", False, f"mcp client unavailable: {exc}")
        return

    expected_safe = get_mcp_client_safe_tools()
    try:
        async with sse_client(
            f"{base_url}/sse",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            sse_read_timeout=10,
        ) as (read, write), ClientSession(read, write) as session:
            init_result = await session.initialize()
            check("MCP initialize over SSE", bool(init_result.protocolVersion),
                  f"protocolVersion={init_result.protocolVersion}")

            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            check("list_tools count == 84", len(tool_names) == 84, f"got {len(tool_names)}")
            check(
                "blocked tool count == 30",
                len(MCP_CLIENT_BLOCKED_TOOLS) == 30,
                f"got {len(MCP_CLIENT_BLOCKED_TOOLS)}",
            )

            overlap = tool_names & MCP_CLIENT_BLOCKED_TOOLS
            check("blocked tools absent from live manifest", not overlap, f"leaked={sorted(overlap)}")
            check("safe tools match tool_modes.get_mcp_client_safe_tools()", tool_names == expected_safe)

            manifest_result = await session.call_tool("tools_manifest", {})
            is_error = getattr(manifest_result, "isError", False)
            check("tools_manifest call over SSE", not is_error)

            health_result = await session.call_tool("health", {})
            health_is_error = getattr(health_result, "isError", False)
            if health_is_error:
                print("  (health tool call failed — non-fatal, likely no live gateway configured)")
    except Exception as exc:  # noqa: BLE001 — protocol-level, report don't crash
        check("MCP protocol over SSE", False, _redact(str(exc)[:200]))


def main() -> int:
    print("=== MCP SSE Safe Smoke ===\n")

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

        check_http_auth(base_url, token)

        import asyncio

        asyncio.run(run_protocol_checks(base_url, token))
    finally:
        stop_server(proc)

    print(f"\n{'=' * 40}\nResults: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
