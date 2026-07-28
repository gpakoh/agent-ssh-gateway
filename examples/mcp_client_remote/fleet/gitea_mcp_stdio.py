"""Gitea MCP stdio entrypoint for opencode agent (opencode.json)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv("/etc/agent-mcp-gitea.env")

from .gitea_server import mcp  # noqa: E402 — load_dotenv must run first

if __name__ == "__main__":
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        raise RuntimeError("GITEA_TOKEN is required")
    mcp.run(transport="stdio")
