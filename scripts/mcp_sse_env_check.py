#!/usr/bin/env python3
"""Static validation for a private SSE env file (Phase 16C).

Checks examples/mcp_server/mcp_client.sse.env (or a given path) BEFORE an
operator runs scripts/mcp_sse_serve.py. Never starts the server, never
connects to anything, never prints token values — only their presence
and length.

Checks:
  - MCP_GATEWAY_TOOL_MODE=mcp_client
  - MCP_CLIENT_SAFE_MODE=true
  - MCP_HTTP_HOST is loopback (127.0.0.1 / localhost / ::1)
  - MCP_HTTP_ALLOW_NON_LOOPBACK is not set to true
  - MCP_HTTP_BEARER_TOKEN is set and the template placeholder was replaced
  - GATEWAY_AGENT_TOKEN / GATEWAY_API_KEY is set and the template
    placeholder was replaced

Usage:
  python3 scripts/mcp_sse_env_check.py [path-to-env-file]

Exits 0 only when every check passes.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT / "examples" / "mcp_server" / "mcp_client.sse.env"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "✅" if ok else "❌"
    suffix = f" — {detail}" if detail else ""
    print(f"  {status} {label}{suffix}")
    if ok:
        PASS += 1
    else:
        FAIL += 1


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser — good enough for the flat template
    shape of mcp_client.sse.env.example. Not a general shell/dotenv parser.
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def is_placeholder(value: str) -> bool:
    return not value or (value.startswith("<") and value.endswith(">"))


def main() -> int:
    print("=== MCP Private SSE Env Check ===\n")

    env_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ENV_FILE
    print(f"Checking: {env_path}\n")

    if not env_path.is_file():
        check("env file exists", False, str(env_path))
        print(f"\nHint: cp examples/mcp_server/mcp_client.sse.env.example {env_path}")
        return 1

    values = parse_env_file(env_path)

    tool_mode = values.get("MCP_GATEWAY_TOOL_MODE", "")
    check(
        "MCP_GATEWAY_TOOL_MODE=mcp_client",
        tool_mode == "mcp_client",
        f"value={tool_mode!r}" if tool_mode else "missing",
    )

    safe_mode = values.get("MCP_CLIENT_SAFE_MODE", "")
    check(
        "MCP_CLIENT_SAFE_MODE=true",
        safe_mode.strip().lower() == "true",
        f"value={safe_mode!r}" if safe_mode else "missing",
    )

    host = values.get("MCP_HTTP_HOST", "")
    is_loopback = host in LOOPBACK_HOSTS
    check(
        "MCP_HTTP_HOST is loopback",
        is_loopback,
        (
            f"value={host!r} — non-loopback binds are forbidden outside a "
            "reviewed, isolated lab"
            if not is_loopback
            else ""
        ),
    )

    allow_non_loopback = values.get("MCP_HTTP_ALLOW_NON_LOOPBACK", "").strip().lower() == "true"
    check(
        "MCP_HTTP_ALLOW_NON_LOOPBACK is not enabled",
        not allow_non_loopback,
        (
            "DANGER: non-loopback bind override is enabled — forbidden "
            "outside a reviewed, temporary, isolated lab with no path to "
            "the public internet"
            if allow_non_loopback
            else ""
        ),
    )

    bearer = values.get("MCP_HTTP_BEARER_TOKEN", "")
    bearer_ok = bool(bearer) and not is_placeholder(bearer)
    check(
        "MCP_HTTP_BEARER_TOKEN set (placeholder replaced)",
        bearer_ok,
        f"length={len(bearer)}" if bearer_ok else "missing or still the template placeholder",
    )

    agent_token = values.get("GATEWAY_AGENT_TOKEN", "") or values.get("GATEWAY_API_KEY", "")
    agent_token_ok = bool(agent_token) and not is_placeholder(agent_token)
    check(
        "GATEWAY_AGENT_TOKEN/GATEWAY_API_KEY set (placeholder replaced)",
        agent_token_ok,
        f"length={len(agent_token)}" if agent_token_ok else "missing or still the template placeholder",
    )

    print(f"\n{'=' * 40}\nResults: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
