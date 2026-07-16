"""Pytest configuration: set env vars before app modules are imported."""

import os
import sys
import tempfile
from pathlib import Path

# MCP server uses bare `from command_policy import ...` which requires
# examples/mcp_server/ on sys.path when imported as a package in tests.
_mcp_dir = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server")
if _mcp_dir not in sys.path:
    sys.path.insert(0, _mcp_dir)

os.environ.setdefault("AUTH_DB_PATH", os.path.join(tempfile.gettempdir(), "test_auth.sqlite3"))
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-testing-only")
os.environ.setdefault("API_KEY", "test-api-key-12345")
os.environ.setdefault("AGENT_TOKEN", "test-agent-token-12345")
os.environ.setdefault("WORKSPACE_READONLY", "false")
