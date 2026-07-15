"""Pytest configuration: set env vars before app modules are imported."""

import os

os.environ.setdefault("AUTH_DB_PATH", "/tmp/test_auth.sqlite3")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-testing-only")
os.environ.setdefault("API_KEY", "test-api-key-12345")
os.environ.setdefault("AGENT_TOKEN", "test-agent-token-12345")
os.environ.setdefault("WORKSPACE_READONLY", "false")
