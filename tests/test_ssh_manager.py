"""Mock-based tests for SSHSessionManager."""
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from app.ssh_manager import SSHSessionManager, SessionNotFoundError, ConnectionError


@pytest.fixture
def mock_paramiko_client():
    client = MagicMock()
    transport = MagicMock()
    transport.is_active.return_value = True
    client.get_transport.return_value = transport
    channel = MagicMock()
    transport.open_session.return_value = channel
    return client


@pytest.fixture
def manager():
    return SSHSessionManager(session_timeout=300, cleanup_interval=60, max_sessions=2)


@pytest.mark.asyncio
async def test_create_session_success(manager, mock_paramiko_client):
    with patch("app.ssh_manager.paramiko.SSHClient") as mock_cls:
        mock_cls.return_value = mock_paramiko_client
        with patch.object(asyncio.get_event_loop(), "run_in_executor", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = None
            sid = await manager.create_session("10.0.0.1", 22, "root", password="secret")
            assert isinstance(sid, str)
            assert len(sid) == 36


@pytest.mark.asyncio
async def test_create_session_max_limit(manager, mock_paramiko_client):
    with patch("app.ssh_manager.paramiko.SSHClient") as mock_cls:
        mock_cls.return_value = mock_paramiko_client
        with patch.object(asyncio.get_event_loop(), "run_in_executor", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = None
            await manager.create_session("host1", 22, "root", password="p")
            await manager.create_session("host2", 22, "root", password="p")
            with pytest.raises(ConnectionError, match="Maximum session limit"):
                await manager.create_session("host3", 22, "root", password="p")


@pytest.mark.asyncio
async def test_execute_success(manager, mock_paramiko_client):
    with patch("app.ssh_manager.paramiko.SSHClient") as mock_cls:
        mock_cls.return_value = mock_paramiko_client
        with patch.object(asyncio.get_event_loop(), "run_in_executor", new_callable=AsyncMock) as mock_exec:
            stdout = MagicMock()
            stdout.read.return_value = b"hello\n"
            stdout.channel.recv_exit_status.return_value = 0
            stderr = MagicMock()
            stderr.read.return_value = b""
            mock_exec.side_effect = [
                None,
                (MagicMock(), stdout, stderr),
                b"hello\n",
                b"",
            ]
            sid = await manager.create_session("host", 22, "root", password="p")
            result = await manager.execute(sid, "echo hello")
            assert result["exit_code"] == 0
            assert result["stdout"] == "hello\n"


@pytest.mark.asyncio
async def test_execute_session_not_found(manager):
    with pytest.raises(SessionNotFoundError):
        await manager.execute("nonexistent-id", "echo test")


@pytest.mark.asyncio
async def test_disconnect_removes_session(manager, mock_paramiko_client):
    with patch("app.ssh_manager.paramiko.SSHClient") as mock_cls:
        mock_cls.return_value = mock_paramiko_client
        with patch.object(asyncio.get_event_loop(), "run_in_executor", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = None
            sid = await manager.create_session("host", 22, "root", password="p")
            await manager.disconnect(sid)
            assert await manager.get_session(sid) is None


@pytest.mark.asyncio
async def test_cleanup_stale_sessions(manager, mock_paramiko_client):
    with patch("app.ssh_manager.paramiko.SSHClient") as mock_cls:
        mock_cls.return_value = mock_paramiko_client
        with patch.object(asyncio.get_event_loop(), "run_in_executor", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = None
            sid = await manager.create_session("host", 22, "root", password="p")
            record = await manager.get_session(sid)
            record.last_activity = 0
            count = await manager.cleanup_stale_sessions()
            assert count == 1
            assert await manager.get_session(sid) is None


@pytest.mark.asyncio
async def test_reconnect_success(manager, mock_paramiko_client):
    with patch("app.ssh_manager.paramiko.SSHClient") as mock_cls:
        mock_cls.return_value = mock_paramiko_client
        with patch.object(asyncio.get_event_loop(), "run_in_executor", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = None
            sid = await manager.create_session("host", 22, "root", password="p")
            record = await manager.get_session(sid)
            record.client.get_transport.return_value = None
            reconnected = await manager.reconnect(sid)
            assert reconnected is True
            assert record.reconnect_count == 1


@pytest.mark.asyncio
async def test_create_pty_channel_success(manager, mock_paramiko_client):
    with patch("app.ssh_manager.paramiko.SSHClient") as mock_cls:
        mock_cls.return_value = mock_paramiko_client
        with patch.object(asyncio.get_event_loop(), "run_in_executor", new_callable=AsyncMock) as mock_exec:
            channel_mock = MagicMock()
            mock_exec.side_effect = [None, channel_mock, None, None]
            sid = await manager.create_session("host", 22, "root", password="p")
            channel = await manager.create_pty_channel(sid)
            assert channel is channel_mock


@pytest.mark.asyncio
async def test_list_sessions(manager, mock_paramiko_client):
    with patch("app.ssh_manager.paramiko.SSHClient") as mock_cls:
        mock_cls.return_value = mock_paramiko_client
        with patch.object(asyncio.get_event_loop(), "run_in_executor", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = None
            await manager.create_session("h1", 22, "root", password="p")
            await manager.create_session("h2", 22, "root", password="p")
            sessions = await manager.list_sessions()
            assert len(sessions) == 2


@pytest.mark.asyncio
async def test_get_session_not_found(manager):
    session = await manager.get_session("nonexistent")
    assert session is None


@pytest.mark.asyncio
async def test_close_all(manager, mock_paramiko_client):
    with patch("app.ssh_manager.paramiko.SSHClient") as mock_cls:
        mock_cls.return_value = mock_paramiko_client
        with patch.object(asyncio.get_event_loop(), "run_in_executor", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = None
            await manager.create_session("h1", 22, "root", password="p")
            await manager.create_session("h2", 22, "root", password="p")
            await manager.close_all()
            sessions = await manager.list_sessions()
            assert len(sessions) == 0


@pytest.mark.asyncio
async def test_disconnect_not_found(manager):
    with pytest.raises(SessionNotFoundError):
        await manager.disconnect("nonexistent")


@pytest.mark.asyncio
async def test_stop_cleanup_task_noop(manager):
    await manager.stop_cleanup_task()
    assert True


@pytest.mark.asyncio
async def test_start_cleanup_task(manager):
    await manager.start_cleanup_task()
    assert manager._cleanup_task is not None
    await manager.stop_cleanup_task()
