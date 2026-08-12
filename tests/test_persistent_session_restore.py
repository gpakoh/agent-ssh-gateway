"""Regression tests for persistent SSH session restart semantics."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.main import _restore_persisted_sessions


@pytest.mark.asyncio
async def test_restore_preserves_session_id_owner_and_tenant_labels(monkeypatch):
    session_store = AsyncMock()
    session_store.list_active_sessions.return_value = [
        {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "owner_type": "agent",
            "owner_name": "agent",
            "owner_token_fingerprint": "fingerprint-123",
            "source_ip": "192.0.2.44",
            "tenant_labels": ["team=a", "env=prod"],
        }
    ]
    session_store.get_session_credentials.return_value = {
        "host": "10.0.0.10",
        "port": 22,
        "username": "deploy",
        "password": "secret",
        "private_key": None,
        "key_passphrase": None,
    }
    manager = AsyncMock()
    manager.create_session.return_value = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr("app.main.validate_target_host", lambda *args, **kwargs: ["10.0.0.10"])

    restored, failed = await _restore_persisted_sessions(session_store, manager)

    assert (restored, failed) == (1, 0)
    kwargs = manager.create_session.await_args.kwargs
    assert kwargs["session_id"] == "11111111-1111-1111-1111-111111111111"
    assert kwargs["owner_type"] == "agent"
    assert kwargs["owner_name"] == "agent"
    assert kwargs["owner_token_fingerprint"] == "fingerprint-123"
    assert kwargs["source_ip"] == "192.0.2.44"
    assert kwargs["tenant_labels"] == ("team=a", "env=prod")
    session_store.refresh_session_expiry.assert_awaited_once_with(
        "11111111-1111-1111-1111-111111111111", settings.session_timeout
    )
    session_store.deactivate_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_refuses_legacy_session_without_owner_fingerprint():
    session_store = AsyncMock()
    session_store.list_active_sessions.return_value = [
        {
            "session_id": "22222222-2222-2222-2222-222222222222",
            "owner_type": "master",
            "owner_name": None,
            "owner_token_fingerprint": None,
            "source_ip": "192.0.2.45",
            "tenant_labels": [],
        }
    ]
    manager = AsyncMock()

    restored, failed = await _restore_persisted_sessions(session_store, manager)

    assert (restored, failed) == (0, 1)
    session_store.deactivate_session.assert_awaited_once_with(
        "22222222-2222-2222-2222-222222222222"
    )
    session_store.get_session_credentials.assert_not_awaited()
    manager.create_session.assert_not_awaited()


def test_session_store_startup_does_not_auto_create_schema():
    source = Path("app/session_store.py").read_text(encoding="utf-8")
    connect_body = source.split("async def connect(self):", 1)[1].split(
        "async def disconnect(self):", 1
    )[0]
    assert "create_all" not in connect_body
    assert "Alembic" in connect_body


def test_persistent_ownership_migration_is_head_and_fresh_db_capable():
    source = Path("alembic/versions/004_persistent_session_ownership.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision: str | None = "003_webhook_delivery_headers"' in source
    assert 'op.create_table(' in source
    for column in (
        "owner_type",
        "owner_name",
        "owner_token_fingerprint",
        "source_ip",
        "tenant_labels",
    ):
        assert column in source
    assert "UPDATE ssh_sessions SET is_active = false" in source
    assert "owner_token_fingerprint IS NULL" in source


def test_deploy_migrates_then_restarts_before_smoke():
    source = Path("scripts/deploy-from-registry.sh").read_text(encoding="utf-8")
    migration = source.index('if ! run_migrations; then')
    restart = source.index('elif ! restart_gateway_after_migrations; then', migration)
    smoke = source.index('elif smoke; then', restart)
    assert migration < restart < smoke


def test_shared_feature_stores_cannot_bootstrap_ssh_sessions_via_shared_base():
    for relative in (
        "app/event_hook_store.py",
        "app/event_hook_delivery.py",
        "app/audit_store.py",
    ):
        source = Path(relative).read_text(encoding="utf-8")
        assert "Base.metadata.create_all" not in source, relative
        assert ".__table__.create" in source, relative
