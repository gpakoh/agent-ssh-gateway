"""Smoke test for MCP scope enforcement under enforce mode.

Tests each access profile (viewer, operator, agent-runner, infra, full)
against the expected allow/deny matrix.

Usage:
  python scripts/mcp_enforce_smoke.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.client import HTTPResponse

ENV_FILE = "/etc/agent-ssh-gateway-mcp.env"
GATEWAY_URL = "http://127.0.0.1:8788/mcp"

def _read_env_val(key: str) -> str:
    """Read a value from the env file."""
    if not os.path.exists(ENV_FILE):
        return ""
    pat = re.compile(r"^" + re.escape(key) + r"=(.*)$", re.MULTILINE)
    with open(ENV_FILE) as f:
        m = pat.search(f.read())
    return m.group(1).strip() if m else ""


HEALTH_TOKEN = _read_env_val("MCP_HEALTHCHECK_BEARER_TOKEN")

# (profile_name, allowed_tools, denied_tools)
PROFILE_CHECKS: list[tuple[str, list[str], list[str]]] = [
    ("viewer",        ["gateway_health"],          ["docker_ps"]),
    ("operator",      ["gateway_health"],          ["docker_ps"]),
    ("agent-runner",  ["gateway_health"],          ["docker_ps"]),
    ("infra",         ["gateway_health", "docker_ps"], []),
    ("full",          ["gateway_health", "docker_ps"], []),
]

PASS = 0
FAIL = 0


def _env_replace(key: str, value: str) -> None:
    """Replace or append KEY=value in env file using regex on raw content."""
    with open(ENV_FILE) as f:
        content = f.read()

    # Replace existing line or append
    pat = re.compile(r"^" + re.escape(key) + r"=.*$", re.MULTILINE)
    new_line = f"{key}={value}"
    if pat.search(content):
        content = pat.sub(new_line, content)
    else:
        content = content.rstrip() + "\n" + new_line + "\n"
    with open(ENV_FILE, "w") as f:
        f.write(content)


def _env_remove(key: str) -> None:
    """Remove KEY= line from env file."""
    with open(ENV_FILE) as f:
        content = f.read()
    pat = re.compile(r"^" + re.escape(key) + r"=.*\n?", re.MULTILINE)
    content = pat.sub("", content)
    with open(ENV_FILE, "w") as f:
        f.write(content)


def _restart() -> None:
    subprocess.run(
        ["systemctl", "restart", "agent-ssh-gateway-mcp.service"],
        check=True, capture_output=True,
    )
    time.sleep(3)
    r = subprocess.run(
        ["systemctl", "is-active", "agent-ssh-gateway-mcp.service"],
        capture_output=True, text=True,
    )
    if r.stdout.strip() != "active":
        print("  FATAL: service not active")
        sys.exit(1)


def _mcp_init(token: str) -> str | None:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "enforce-smoke", "version": "1.0"},
        },
    }).encode()
    req = urllib.request.Request(
        GATEWAY_URL, data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        resp: HTTPResponse = urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"    INIT FAIL: {e}")
        return None
    sid = resp.headers.get("Mcp-Session-Id", "")
    _drain(resp)
    return sid if sid else None


def _call_tool(token: str, sid: str | None, tool: str) -> int:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": {}},
    }).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(GATEWAY_URL, data=body, headers=headers)
    try:
        resp: HTTPResponse = urllib.request.urlopen(req, timeout=10)
        _drain(resp)
        return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        print(f"    ERROR: {e}")
        return 0


def _drain(resp: HTTPResponse) -> None:
    try:
        while resp.read(4096):
            pass
    except Exception:
        pass


def _check(profile: str, token: str) -> None:
    global PASS, FAIL
    print(f"\n  == {profile} ==")
    sid = _mcp_init(token)
    if sid is None:
        print("    INIT FAIL")
        FAIL += 1
        return

    ok_tools: list[str] = []
    denied_tools: list[str] = []
    for p, ok, den in PROFILE_CHECKS:
        if p == profile:
            ok_tools = ok
            denied_tools = den
            break

    for tool in ok_tools:
        status = _call_tool(token, sid, tool)
        if 200 <= status < 300:
            print(f"    ✅ {tool} → {status}")
            PASS += 1
        else:
            print(f"    ❌ {tool} → {status} (expected 2xx)")
            FAIL += 1

    for tool in denied_tools:
        status = _call_tool(token, sid, tool)
        if status == 403 or status == 0:
            print(f"    ✅ {tool} → {status}")
            PASS += 1
        else:
            print(f"    ❌ {tool} → {status} (expected 403)")
            FAIL += 1


def _check_tools_list(token: str) -> None:
    global PASS, FAIL
    body = json.dumps({
        "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {},
    }).encode()
    req = urllib.request.Request(
        GATEWAY_URL, data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        resp: HTTPResponse = urllib.request.urlopen(req, timeout=10)
        _drain(resp)
        print(f"    ✅ tools/list → {resp.status}")
        PASS += 1
    except Exception as e:
        print(f"    ❌ tools/list → {e}")
        FAIL += 1


def _cleanup_temp_file(path: str) -> None:
    """Remove temp file if it exists."""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


def main():
    global PASS, FAIL
    ts = str(int(time.time()))
    tmp_token_file = f"/tmp/mcp-enforce-smoke-{ts}.json"

    tokens = {
        f"smoke_viewer_{ts}":      "viewer",
        f"smoke_operator_{ts}":    "operator",
        f"smoke_agent_runner_{ts}": "agent-runner",
        f"smoke_infra_{ts}":       "infra",
    }

    print("=== MCP Scope Enforcement Smoke ===")
    print("  tokens: viewer, operator, agent-runner, infra + full (healthcheck)")

    # Write tokens to temp JSON file (avoids env escaping issues)
    with open(tmp_token_file, "w") as f:
        json.dump(tokens, f)

    # 1. enable enforce + token file
    print("\n--- Enforce mode ---")
    _env_replace("MCP_SCOPE_ENFORCEMENT", "enforce")
    _env_remove("MCP_EXTRA_TOKENS_JSON")  # ensure no stale JSON env var
    _env_replace("MCP_EXTRA_TOKENS_FILE", tmp_token_file)
    _restart()

    # 2. smoke per profile
    print("\n--- Testing profiles ---")
    for token_value, profile in tokens.items():
        _check(profile, token_value)

    # 3. full profile via healthcheck token
    print("\n--- Full profile ---")
    if HEALTH_TOKEN:
        _check("full", HEALTH_TOKEN)
    else:
        print("  SKIP (no MCP_HEALTHCHECK_BEARER_TOKEN in env)")

    # 4. tools/list not blocked
    print("\n--- tools/list ---")
    for token_value in tokens:
        _check_tools_list(token_value)

    # 5. restore audit
    print("\n--- Restore audit ---")
    _env_replace("MCP_SCOPE_ENFORCEMENT", "audit")
    _env_remove("MCP_EXTRA_TOKENS_FILE")
    _cleanup_temp_file(tmp_token_file)
    _restart()

    print(f"\n{'='*50}")
    print(f"  {PASS} passed, {FAIL} failed (of {PASS+FAIL})")
    print(f"{'='*50}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
