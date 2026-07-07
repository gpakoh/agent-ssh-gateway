"""Entry point for agent-mcp-postgres.service."""

from __future__ import annotations

import os
import sys
import threading

import uvicorn

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fleet"))

from fleet.postgres_server import create_auth_proxy, mcp
from fleet.shared import get_fleet_env

env = get_fleet_env()
internal_port = int(os.environ.get("MCP_INTERNAL_PORT", "8784"))
public_port = env["port"]

mcp.settings.host = "127.0.0.1"
mcp.settings.port = internal_port

t = threading.Thread(
    target=mcp.run,
    kwargs={"transport": "streamable-http"},
    daemon=True,
)
t.start()

app = create_auth_proxy(upstream_port=internal_port, valid_tokens={env["token"]})
uvicorn.run(app, host=env["host"], port=public_port)
