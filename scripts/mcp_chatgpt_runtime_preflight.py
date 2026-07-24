#!/usr/bin/env python3
"""MCP ChatGPT safe mode runtime preflight check.

Verifies the environment is correctly configured for ChatGPT safe attach.
Exits non-zero on any unsafe configuration.

Env vars checked (from process only, not from files):
  GATEWAY_URL              required
  GATEWAY_AGENT_TOKEN      required (never printed)
  MCP_GATEWAY_TOOL_MODE    must be "chatgpt"
  MCP_CHATGPT_SAFE_MODE    must be "true"
  MCP_ACCESS_PROFILE       must be "chatgpt_safe" or unset
"""

from __future__ import annotations

import os
import sys

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


def main() -> int:
    print("=== MCP ChatGPT Runtime Preflight ===\n")

    # 1. Required env vars
    url = os.environ.get("GATEWAY_URL", "")
    token = os.environ.get("GATEWAY_AGENT_TOKEN", "")
    mode = os.environ.get("MCP_GATEWAY_TOOL_MODE", "")
    safe = os.environ.get("MCP_CHATGPT_SAFE_MODE", "")
    profile = os.environ.get("MCP_ACCESS_PROFILE", "")

    check("GATEWAY_URL present", bool(url), "set" if url else "missing")
    check("GATEWAY_AGENT_TOKEN present", bool(token), f"length={len(token)}" if token else "missing")
    check("GATEWAY_AGENT_TOKEN not printed", True)

    # 2. Safe mode config
    check("MCP_GATEWAY_TOOL_MODE=chatgpt", mode == "chatgpt"), f"value={mode!r}" if mode else "missing"
    check("MCP_CHATGPT_SAFE_MODE=true", safe.lower() in ("true", "1", "yes"), f"value={safe!r}" if safe else "missing")
    check("MCP_ACCESS_PROFILE=chatgpt_safe", profile == "chatgpt_safe" or not profile, f"value={profile!r}")

    # 3. Import tool mode and verify dangerous tools excluded
    if mode == "chatgpt" and safe.lower() in ("true", "1", "yes"):
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples", "mcp_server"))
            from tool_modes import CHATGPT_BLOCKED_TOOLS, get_chatgpt_safe_tools, is_chatgpt_safe_mode  # noqa: E402, I001

            check("is_chatgpt_safe_mode() returns True", is_chatgpt_safe_mode())
            safe_tools = get_chatgpt_safe_tools()
            blocked = CHATGPT_BLOCKED_TOOLS
            check("safe set non-empty", len(safe_tools) > 0, f"{len(safe_tools)} tools")
            check("blocked set non-empty", len(blocked) > 0, f"{len(blocked)} tools")
            check("no overlap", len(safe_tools & blocked) == 0)

            for dangerous in ["project_run_opencode", "project_run_mimo", "project_run_agent",
                              "docker_exec", "docker_compose_up", "workspace_file_write",
                              "workspace_apply_patch"]:
                check(f"{dangerous} excluded", dangerous not in safe_tools)

        except ImportError as e:
            check("import tool_modes", False, str(e))

    # 4. Optional: gateway health check
    if url and token:
        try:
            import urllib.request

            req = urllib.request.Request(f"{url.rstrip('/')}/health", headers={"X-API-Key": token})
            with urllib.request.urlopen(req, timeout=5) as resp:
                check("gateway /health reachable", resp.status == 200, f"HTTP {resp.status}")
        except Exception as e:
            check("gateway /health reachable", False, str(e))
    else:
        print("\n  (Skipping gateway health — set GATEWAY_URL and GATEWAY_AGENT_TOKEN to enable)")

    print(f"\n{'='*40}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
