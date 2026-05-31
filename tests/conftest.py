"""Shared fixtures for all tests — resets global state to prevent cross-test pollution."""

import importlib
import pytest


@pytest.fixture(autouse=True)
def reset_app_state_globals():
    """Reset all module-level state globals to None after each test."""
    import app.state as state

    saved = {}
    for name in _STATE_NAMES:
        saved[name] = getattr(state, name, None)
    yield
    for name in _STATE_NAMES:
        setattr(state, name, None)
    state.active_websockets.clear()


@pytest.fixture(autouse=True)
def reset_fastapi_dependency_overrides():
    """Clear FastAPI dependency_overrides after each test."""
    try:
        from app.main import app
    except Exception:
        yield
        return

    old = dict(app.dependency_overrides)
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(old)


_STATE_NAMES = [
    "manager", "job_manager", "file_editor", "context_manager",
    "batch_manager", "code_intelligence", "search_replace",
    "file_tree", "server_manager", "snapshot_manager",
    "webhook_manager", "analytics",
    "audit_logger", "redis_queue", "circuit_breakers",
    "dist_lock", "session_store", "host_key_store",
    "bulk_ops", "event_hook_store", "delivery_service",
    "agent_token_store",
]
