"""Pytest configuration: set env vars before app modules are imported."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

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
os.environ.setdefault("SETUP_TOKEN", "test-setup-token-12345")
# examples/mcp_server/config.py has no built-in project-root allowlist: it is a
# security boundary and the roots are deployment configuration. Provide a
# neutral, hermetic default so the suite does not depend on the ambient host
# layout (tests that assert the fail-closed behaviour override this explicitly).
os.environ.setdefault("MCP_ALLOWED_PROJECT_ROOTS", "/tmp/nod-test-project-roots/")
os.environ.setdefault("GPT_BRIDGE_IMAGE_REPO", "registry.example.com/test/gpt-browser-bridge")


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear the shared in-memory rate-limit storage between tests."""
    from app.security import limiter

    limiter.reset()
    yield
