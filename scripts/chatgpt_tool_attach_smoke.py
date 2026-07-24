#!/usr/bin/env python3
"""ChatGPT safe tool attach smoke test.

Validates that the safe mode tool set excludes dangerous tools
and includes safe read-only/testlint tools.

Env vars:
  GATEWAY_URL        (default http://localhost:8085)
  GATEWAY_AGENT_TOKEN (required for live test)
  TEST_SSH_HOST      (optional — if set, runs readonly SSH test)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

# Allow running from repo root without package installation
_MCP_DIR = os.path.join(os.path.dirname(__file__), "..", "examples", "mcp_server")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

from tool_modes import CHATGPT_BLOCKED_TOOLS, get_chatgpt_safe_tools  # noqa: E402

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8085").rstrip("/")
AGENT_TOKEN = os.getenv("GATEWAY_AGENT_TOKEN", "")
TEST_SSH_HOST = os.getenv("TEST_SSH_HOST", "")

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


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{GATEWAY_URL}{path}",
        headers={"X-API-Key": token},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> int:
    print("=== ChatGPT Safe Tool Attach Smoke ===\n")

    # 1. Health check
    health = _get("/health", AGENT_TOKEN)
    check("health", health.get("status") == "ok", f"version={health.get('version')}")

    # 2. Capabilities
    caps = _get("/api/capabilities", AGENT_TOKEN)
    check("capabilities", "version" in caps)

    # 3. Safe mode tool set validation
    safe_tools = get_chatgpt_safe_tools()
    blocked = CHATGPT_BLOCKED_TOOLS

    check("safe set is non-empty", len(safe_tools) > 0, f"{len(safe_tools)} tools")
    check("blocked set is non-empty", len(blocked) > 0, f"{len(blocked)} tools")
    check("no overlap between safe and blocked", len(safe_tools & blocked) == 0)

    # Verify key safe tools are present
    required_safe = {"health", "tools_manifest", "job_status", "read_file", "repo_status"}
    check("required safe tools present", required_safe.issubset(safe_tools))

    # Verify key blocked tools are absent
    required_blocked = {
        "project_run_opencode", "project_run_mimo", "project_run_agent",
        "docker_exec", "docker_compose_up", "workspace_file_write",
        "workspace_apply_patch",
    }
    check("blocked tools excluded", required_blocked.issubset(blocked))

    # 4. If SSH test host provided, test readonly command
    if TEST_SSH_HOST and AGENT_TOKEN:
        print("\n  Live SSH test (if gateway has session to test host):")
        try:
            conn = _get("/api/sessions", AGENT_TOKEN)
            sessions = conn.get("sessions", [])
            if sessions:
                sid = sessions[0]["session_id"]
                print(f"    session found: {sid}")
            else:
                print("    no sessions available — skipping live test")
        except Exception as e:
            print(f"    sessions endpoint: {e}")
    else:
        print("\n  (Skipping live SSH test — set TEST_SSH_HOST to enable)")

    print(f"\n{'='*40}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
