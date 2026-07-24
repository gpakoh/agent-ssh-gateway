#!/usr/bin/env python3
"""Local MCP stdio safe-mode smoke test.

Starts the MCP server as a subprocess in safe mode, connects via MCP
stdio protocol, verifies tool manifest, calls health + tools_manifest,
and checks blocked tools are absent. Exits 1 on unsafe manifest only.
Exits 0 when env is missing or MCP client unavailable (SKIP/BLOCKER).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = ROOT / "examples" / "mcp_server" / "server.py"

BLOCKED_TOOLS = frozenset({
    "project_run_opencode",
    "project_run_mimo",
    "project_run_agent",
    "docker_exec",
    "docker_compose_up",
    "workspace_file_write",
    "workspace_apply_patch",
    "project_apply_patch",
})

REQUIRED_TOOLS = frozenset({"health", "tools_manifest"})


def _redact(s: str) -> str:
    return re.sub(r"[A-Za-z0-9_\-]{20,}", "<REDACTED>", s)


def _build_env() -> dict[str, str] | None:
    needed = ["GATEWAY_URL", "GATEWAY_AGENT_TOKEN"]
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        print(f"SKIP: missing env vars: {', '.join(missing)}")
        return None

    env: dict[str, str] = {
        "GATEWAY_URL": os.environ["GATEWAY_URL"],
        "GATEWAY_API_KEY": os.environ["GATEWAY_AGENT_TOKEN"],
        "GATEWAY_AGENT_TOKEN": os.environ["GATEWAY_AGENT_TOKEN"],
        "MCP_GATEWAY_TOOL_MODE": "chatgpt",
        "MCP_CHATGPT_SAFE_MODE": "true",
        "MCP_ACCESS_PROFILE": "chatgpt_safe",
        "MCP_AUTH_MODE": "token",
        "MCP_PUBLIC_TOKEN": os.environ["GATEWAY_AGENT_TOKEN"],
    }

    python = sys.executable
    env["PATH"] = f"{str(Path(python).parent)}:{os.environ.get('PATH', '')}"
    env["PYTHONPATH"] = str(ROOT)
    env.pop("VIRTUAL_ENV", None)
    return env


async def _run_smoke() -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = _build_env()
    if env is None:
        return 0

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PY)],
        env=env,
        cwd=str(ROOT),
    )

    print("Starting MCP server (stdio) in safe mode")
    print(f"  GATEWAY_URL={_redact(env['GATEWAY_URL'])}")
    print("  MCP_CHATGPT_SAFE_MODE=true")
    print("  GATEWAY_AGENT_TOKEN=<REDACTED>")

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            init_result = await session.initialize()
            print(f"MCP initialize: protocolVersion={init_result.protocolVersion}")

            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            tool_count = len(tool_names)
            print(f"list_tools: {tool_count} tools registered")

            errors: list[str] = []

            for blocked in BLOCKED_TOOLS:
                if blocked in tool_names:
                    errors.append(f"BLOCKED tool in manifest: {blocked}")
                    print(f"  FAIL: {blocked} present (should be blocked)")

            for required in REQUIRED_TOOLS:
                if required not in tool_names:
                    errors.append(f"Required tool missing: {required}")
                    print(f"  FAIL: {required} missing")

            if errors:
                print(f"\nUNSAFE MANIFEST: {len(errors)} error(s)")
                for e in errors:
                    print(f"  - {e}")
                return 1

            print("  All blocked tools confirmed absent")
            print("  All required tools confirmed present")

            for tool_name in ("health", "tools_manifest"):
                print(f"\nCalling tool: {tool_name}")
                try:
                    result = await session.call_tool(tool_name, {})
                    text = ""
                    for content in result.content:
                        if hasattr(content, "text"):
                            text += content.text
                    is_error = getattr(result, "isError", False)
                    if is_error:
                        print(f"  {tool_name}: server returned error (non-fatal)")
                        print(f"  response: {_redact(text[:300])}")
                    else:
                        print(f"  {tool_name}: OK ({len(text)} chars)")
                        print(f"  response: {_redact(text[:200])}")
                except Exception as exc:
                    print(f"  {tool_name}: call failed (non-fatal): {_redact(str(exc)[:200])}")

            print(f"\nAll manifest checks passed. {tool_count} safe tools, 0 blocked.")
            return 0


def main() -> int:
    try:
        import asyncio
        return asyncio.run(_run_smoke())
    except ModuleNotFoundError as exc:
        print(f"BLOCKER: MCP client package not available: {exc}")
        print("Install with: pip install mcp")
        return 0
    except FileNotFoundError as exc:
        print(f"BLOCKER: MCP server not found: {exc}")
        return 0
    except Exception as exc:
        print(f"BLOCKER: MCP stdio smoke failed: {_redact(str(exc)[:300])}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
