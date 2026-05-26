"""Integration tests for SSHSessionManager using real SSH connection."""

import pytest

from app.ssh_manager import SSHSessionManager

pytestmark = pytest.mark.integration


@pytest.fixture
def manager():
    return SSHSessionManager(session_timeout=300, cleanup_interval=60, max_sessions=10)


@pytest.mark.asyncio
async def test_real_execute_command(sshd_server, manager):
    sid = await manager.create_session(
        host=sshd_server["host"],
        port=sshd_server["port"],
        username=sshd_server["username"],
        private_key=sshd_server["private_key"],
    )
    try:
        result = await manager.execute(sid, "echo test_ok")
        assert result["stdout"].strip() == "test_ok"
        assert result["exit_code"] == 0
    finally:
        await manager.disconnect(sid)


@pytest.mark.asyncio
async def test_real_key_auth(sshd_server, manager):
    with open("/tmp/ssh_test_key") as f:
        private_key = f.read()
    sid = await manager.create_session(
        host=sshd_server["host"],
        port=sshd_server["port"],
        username=sshd_server["username"],
        private_key=private_key,
    )
    try:
        result = await manager.execute(sid, "whoami")
        assert result["stdout"].strip() == "root"
        assert result["exit_code"] == 0
    finally:
        await manager.disconnect(sid)


@pytest.mark.asyncio
async def test_real_reconnect(sshd_server, manager):
    sid = await manager.create_session(
        host=sshd_server["host"],
        port=sshd_server["port"],
        username=sshd_server["username"],
        private_key=sshd_server["private_key"],
    )
    try:
        record = await manager.get_session(sid)
        record.client.close()
        assert not record.is_connected()

        reconnected = await manager.reconnect(sid)
        assert reconnected is True
        assert record.is_connected()
        assert record.reconnect_count == 1

        result = await manager.execute(sid, "echo reconnected_ok")
        assert result["stdout"].strip() == "reconnected_ok"
    finally:
        await manager.disconnect(sid)


@pytest.mark.asyncio
async def test_real_execute_stream(sshd_server, manager):
    sid = await manager.create_session(
        host=sshd_server["host"],
        port=sshd_server["port"],
        username=sshd_server["username"],
        private_key=sshd_server["private_key"],
    )
    try:
        chunks = []
        async for chunk_type, data in manager.execute_stream(sid, "seq 1 5"):
            chunks.append((chunk_type, data))

        stdout_data = "".join(data for t, data in chunks if t == "stdout")
        lines = stdout_data.strip().split("\n")
        assert lines == ["1", "2", "3", "4", "5"]

        exit_chunks = [data for t, data in chunks if t == "exit"]
        assert exit_chunks == ["0"]
    finally:
        await manager.disconnect(sid)
