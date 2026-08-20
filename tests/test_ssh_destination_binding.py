from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.ssh_manager as ssh_manager
from app.ssh_manager import SSHSessionManager


class _FakeSocket:
    def __init__(self, peer_ip: str, port: int) -> None:
        self._peer = (peer_ip, port)
        self.closed = False

    def getpeername(self):
        return self._peer

    def close(self) -> None:
        self.closed = True


class _FakeTransport:
    def __init__(self, sock: _FakeSocket) -> None:
        self.sock = sock
        self._active = True
        self.window_size = 0
        self.packetizer = SimpleNamespace(REKEY_BYTES=0, REKEY_PACKETS=0)

    def is_active(self) -> bool:
        return self._active


class _FakeSSHClient:
    instances: list[_FakeSSHClient] = []

    def __init__(self) -> None:
        self.transport: _FakeTransport | None = None
        self.hostname: str | None = None
        self.host_key_policy = None
        type(self).instances.append(self)

    def set_missing_host_key_policy(self, policy) -> None:
        self.host_key_policy = policy

    def connect(self, *, hostname: str, sock=None, **kwargs) -> None:
        assert sock is not None
        self.hostname = hostname
        self.transport = _FakeTransport(sock)

    def get_transport(self):
        return self.transport

    def close(self) -> None:
        if self.transport is not None:
            self.transport._active = False


def _install_fake_transport(monkeypatch: pytest.MonkeyPatch):
    socket_calls: list[tuple[str, int]] = []
    _FakeSSHClient.instances.clear()

    def fake_create_connection(address, timeout):
        host, port = address
        socket_calls.append((host, port))
        return _FakeSocket(host, port)

    monkeypatch.setattr(ssh_manager.socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(ssh_manager.paramiko, "SSHClient", _FakeSSHClient)
    monkeypatch.setattr(ssh_manager, "_emit", lambda *args, **kwargs: None)
    return socket_calls


@pytest.mark.asyncio
async def test_pool_lifecycle_never_reuses_peer_outside_current_pinned_destination(
    monkeypatch: pytest.MonkeyPatch,
):
    """TEST-11: real manager+pool lifecycle must bind reuse to the current destination."""
    socket_calls = _install_fake_transport(monkeypatch)
    manager = SSHSessionManager(connection_pool_size=2)

    first_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        owner_type="master",
        owner_token_fingerprint="owner-a",
        source_ip="198.51.100.10",
        pinned_ip="192.0.2.10",
    )
    first = await manager.get_session(first_id)
    assert first is not None
    assert first.client.get_transport().sock.getpeername()[0] == "192.0.2.10"

    await manager.disconnect(first_id)
    assert manager.pool_stats is not None
    assert manager.pool_stats["idle"] == 1

    second_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        owner_type="master",
        owner_token_fingerprint="owner-a",
        source_ip="198.51.100.10",
        pinned_ip="192.0.2.20",
    )
    second = await manager.get_session(second_id)
    assert second is not None

    actual_peer = second.client.get_transport().sock.getpeername()[0]
    assert actual_peer == "192.0.2.20", (
        f"current request pinned 192.0.2.20 but pooled transport peer is {actual_peer}"
    )
    assert socket_calls == [("192.0.2.10", 22), ("192.0.2.20", 22)]


@pytest.mark.asyncio
async def test_concurrent_creates_cannot_share_one_pooled_transport(
    monkeypatch: pytest.MonkeyPatch,
):
    socket_calls = _install_fake_transport(monkeypatch)
    manager = SSHSessionManager(connection_pool_size=2)

    seed_id = await manager.create_session(
        host="target.example", port=22, username="alice", password="secret", pinned_ip="192.0.2.10"
    )
    seed = await manager.get_session(seed_id)
    assert seed is not None
    seed_client = seed.client
    await manager.disconnect(seed_id)

    first_id, second_id = await __import__("asyncio").gather(
        manager.create_session(
            host="target.example", port=22, username="alice", password="secret", pinned_ip="192.0.2.10"
        ),
        manager.create_session(
            host="target.example", port=22, username="alice", password="secret", pinned_ip="192.0.2.10"
        ),
    )
    first = await manager.get_session(first_id)
    second = await manager.get_session(second_id)
    assert first is not None and second is not None
    assert first.client is not second.client
    assert seed_client in (first.client, second.client)
    assert socket_calls == [("192.0.2.10", 22), ("192.0.2.10", 22)]


@pytest.mark.asyncio
async def test_transport_reuse_does_not_transfer_logical_owner_or_source(
    monkeypatch: pytest.MonkeyPatch,
):
    socket_calls = _install_fake_transport(monkeypatch)
    manager = SSHSessionManager(connection_pool_size=2)

    first_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        owner_type="agent",
        owner_name="first",
        owner_token_fingerprint="owner-a",
        source_ip="198.51.100.10",
        pinned_ip="192.0.2.10",
    )
    first = await manager.get_session(first_id)
    assert first is not None
    first_client = first.client
    await manager.disconnect(first_id)

    second_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        owner_type="agent",
        owner_name="second",
        owner_token_fingerprint="owner-b",
        source_ip="198.51.100.20",
        pinned_ip="192.0.2.10",
    )
    second = await manager.get_session(second_id)
    assert second is not None
    assert second.client is first_client
    assert second.owner_token_fingerprint == "owner-b"
    assert second.source_ip == "198.51.100.20"
    assert socket_calls == [("192.0.2.10", 22)]

    assert (
        await manager.find_reusable_session(
            host="target.example",
            port=22,
            username="alice",
            password="secret",
            owner_type="agent",
            owner_token_fingerprint="owner-a",
            source_ip="198.51.100.10",
            validated_ips=("192.0.2.10",),
        )
        is None
    )


@pytest.mark.asyncio
async def test_persistent_restore_respects_new_validated_destination(
    monkeypatch: pytest.MonkeyPatch,
):
    from unittest.mock import AsyncMock

    import app.main as app_main

    socket_calls = _install_fake_transport(monkeypatch)
    manager = SSHSessionManager(connection_pool_size=2)

    old_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        owner_type="agent",
        owner_token_fingerprint="owner-a",
        source_ip="198.51.100.10",
        pinned_ip="192.0.2.10",
    )
    await manager.disconnect(old_id)

    restore_id = "11111111-1111-1111-1111-111111111111"
    store = AsyncMock()
    store.list_active_sessions.return_value = [
        {
            "session_id": restore_id,
            "owner_type": "agent",
            "owner_name": "agent-a",
            "owner_token_fingerprint": "owner-a",
            "source_ip": "198.51.100.10",
            "tenant_labels": [],
        }
    ]
    store.get_session_credentials.return_value = {
        "host": "target.example",
        "port": 22,
        "username": "alice",
        "password": "secret",
        "private_key": None,
        "key_passphrase": None,
    }
    monkeypatch.setattr(app_main, "validate_target_host", lambda *args, **kwargs: ["192.0.2.20"])

    restored, failed = await app_main._restore_persisted_sessions(store, manager)
    assert (restored, failed) == (1, 0)
    record = await manager.get_session(restore_id)
    assert record is not None
    assert record.destination_ip == "192.0.2.20"
    assert record.client.get_transport().sock.getpeername()[0] == "192.0.2.20"
    assert socket_calls == [("192.0.2.10", 22), ("192.0.2.20", 22)]


@pytest.mark.asyncio
async def test_fresh_connect_rejects_socket_peer_that_differs_from_pinned_destination(
    monkeypatch: pytest.MonkeyPatch,
):
    _FakeSSHClient.instances.clear()
    monkeypatch.setattr(
        ssh_manager.socket,
        "create_connection",
        lambda address, timeout: _FakeSocket("192.0.2.99", address[1]),
    )
    monkeypatch.setattr(ssh_manager.paramiko, "SSHClient", _FakeSSHClient)
    monkeypatch.setattr(ssh_manager, "_emit", lambda *args, **kwargs: None)
    manager = SSHSessionManager(connection_pool_size=0)

    with pytest.raises(ssh_manager.ConnectionError, match="pinned destination"):
        await manager.create_session(
            host="target.example",
            port=22,
            username="alice",
            password="secret",
            pinned_ip="192.0.2.10",
        )


@pytest.mark.asyncio
async def test_logical_reuse_requires_current_validated_destination_compatibility(
    monkeypatch: pytest.MonkeyPatch,
):
    socket_calls = _install_fake_transport(monkeypatch)
    manager = SSHSessionManager(connection_pool_size=0)

    session_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        owner_type="master",
        owner_token_fingerprint="owner-a",
        source_ip="198.51.100.10",
        pinned_ip="192.0.2.10",
    )
    record = await manager.get_session(session_id)
    assert record is not None
    assert record.client.get_transport().sock.getpeername()[0] == "192.0.2.10"
    assert socket_calls == [("192.0.2.10", 22)]

    reusable = await manager.find_reusable_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        owner_type="master",
        owner_token_fingerprint="owner-a",
        source_ip="198.51.100.10",
        validated_ips=("192.0.2.20",),
    )
    assert reusable is None


@pytest.mark.asyncio
async def test_pool_reuses_same_ipv4_destination(
    monkeypatch: pytest.MonkeyPatch,
):
    socket_calls = _install_fake_transport(monkeypatch)
    manager = SSHSessionManager(connection_pool_size=2)

    first_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        pinned_ip="192.0.2.10",
    )
    first = await manager.get_session(first_id)
    assert first is not None
    first_client = first.client
    await manager.disconnect(first_id)

    second_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        pinned_ip="192.0.2.10",
    )
    second = await manager.get_session(second_id)
    assert second is not None
    assert second.client is first_client
    assert second.destination_ip == "192.0.2.10"
    assert socket_calls == [("192.0.2.10", 22)]


@pytest.mark.asyncio
async def test_pool_ipv6_identity_is_canonicalized(
    monkeypatch: pytest.MonkeyPatch,
):
    socket_calls = _install_fake_transport(monkeypatch)
    manager = SSHSessionManager(connection_pool_size=2)

    first_id = await manager.create_session(
        host="v6.example",
        port=22,
        username="alice",
        password="secret",
        pinned_ip="2001:db8::1",
    )
    first = await manager.get_session(first_id)
    assert first is not None
    first_client = first.client
    assert first.destination_ip == "2001:db8::1"
    await manager.disconnect(first_id)

    second_id = await manager.create_session(
        host="v6.example",
        port=22,
        username="alice",
        password="secret",
        pinned_ip="2001:0db8:0:0:0:0:0:1",
    )
    second = await manager.get_session(second_id)
    assert second is not None
    assert second.client is first_client
    assert second.destination_ip == "2001:db8::1"
    assert socket_calls == [("2001:db8::1", 22)]


@pytest.mark.asyncio
async def test_logical_reuse_accepts_same_destination_in_current_validated_set(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_transport(monkeypatch)
    manager = SSHSessionManager(connection_pool_size=0)

    session_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        owner_type="master",
        owner_token_fingerprint="owner-a",
        source_ip="198.51.100.10",
        pinned_ip="192.0.2.10",
    )
    record = await manager.get_session(session_id)
    assert record is not None

    reusable = await manager.find_reusable_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        owner_type="master",
        owner_token_fingerprint="owner-a",
        source_ip="198.51.100.10",
        validated_ips=("192.0.2.20", "192.0.2.10"),
    )
    assert reusable is record


@pytest.mark.asyncio
async def test_pool_disabled_always_dials_current_destination(
    monkeypatch: pytest.MonkeyPatch,
):
    socket_calls = _install_fake_transport(monkeypatch)
    manager = SSHSessionManager(connection_pool_size=0)

    first_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        pinned_ip="192.0.2.10",
    )
    await manager.disconnect(first_id)

    second_id = await manager.create_session(
        host="target.example",
        port=22,
        username="alice",
        password="secret",
        pinned_ip="192.0.2.20",
    )
    second = await manager.get_session(second_id)
    assert second is not None
    assert second.client.get_transport().sock.getpeername()[0] == "192.0.2.20"
    assert socket_calls == [("192.0.2.10", 22), ("192.0.2.20", 22)]
