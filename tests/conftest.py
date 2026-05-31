"""Global test fixtures — autouse cleanup to prevent cross-file state pollution."""

import gc
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    """Restore FastAPI dependency_overrides after each test module."""
    from app.main import app
    old = dict(app.dependency_overrides)
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(old)
