"""Mock-based tests for SessionStore."""
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch, AsyncMock

from app.session_store import SessionStore, PersistentSession


class _AsyncCM:
    """Async context manager that returns a mock session."""

    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def store():
    s = SessionStore("postgresql+asyncpg://test:test@localhost/test")
    s._engine = AsyncMock()
    mock_session = AsyncMock()
    mock_session.execute.return_value = MagicMock()
    s._session_maker = MagicMock(return_value=_AsyncCM(mock_session))
    return s


@pytest.fixture
def mock_session(store):
    cm = store._session_maker.return_value
    return cm.session


@pytest.mark.asyncio
async def test_save_session_encrypts_password(store, mock_session):
    with patch("app.security.SecretManager") as mock_secret_cls:
        mock_secret_cls.return_value.encrypt.return_value = "enc_pass"

        await store.save_session(
            session_id="test-id",
            host="127.0.0.1",
            port=22,
            username="root",
            password="secret123",
        )

        mock_session.merge.assert_called_once()
        merged_obj = mock_session.merge.call_args[0][0]
        assert isinstance(merged_obj, PersistentSession)
        assert merged_obj.password_encrypted == "enc_pass"
        assert merged_obj.session_id == "test-id"
        assert merged_obj.host == "127.0.0.1"
        assert merged_obj.port == 22
        assert merged_obj.username == "root"
        mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_get_session_active(store, mock_session):
    db_session = PersistentSession(
        session_id="test-id-123",
        host="10.0.0.1",
        port=2222,
        username="admin",
        expires_at=datetime(2030, 1, 1),
        is_active=True,
    )
    mock_session.execute.return_value.scalar_one_or_none.return_value = db_session

    result = await store.get_session("test-id-123")

    assert result is not None
    assert result["session_id"] == "test-id-123"
    assert result["host"] == "10.0.0.1"
    assert result["port"] == 2222
    assert result["username"] == "admin"
    assert result["is_active"] is True
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_get_session_expired(store, mock_session):
    mock_session.execute.return_value.scalar_one_or_none.return_value = None

    result = await store.get_session("nonexistent")

    assert result is None


@pytest.mark.asyncio
async def test_deactivate_session(store, mock_session):
    db_session = PersistentSession(
        session_id="test-id",
        host="10.0.0.1",
        port=22,
        username="root",
        expires_at=datetime(2030, 1, 1),
        is_active=True,
    )
    mock_session.execute.return_value.scalar_one_or_none.return_value = db_session

    await store.deactivate_session("test-id")

    assert db_session.is_active is False
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_expired_sessions(store, mock_session):
    mock_session.execute.return_value.rowcount = 5

    with patch("sqlalchemy.delete") as mock_delete:
        mock_delete.return_value.where.return_value = "mocked_stmt"

        count = await store.cleanup_expired_sessions()

        assert count == 5
        mock_delete.assert_called_once_with(PersistentSession)
        mock_delete.return_value.where.assert_called_once()
        mock_session.commit.assert_called_once()
