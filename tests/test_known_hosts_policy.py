"""Tests for KnownHostsPolicy integration (fail-closed)."""

import asyncio
import base64

import paramiko
import pytest

from app.known_hosts import HostKeyStore, KnownHostsPolicy


class InMemoryHostKeyStore(HostKeyStore):
    """Simple in-memory store for policy tests."""

    def __init__(self):
        self._keys: dict[tuple[str, int], str] = {}

    async def check(self, host: str, port: int, key: paramiko.PKey):
        if key is None:
            return None
        key_data = base64.b64encode(key.asbytes()).decode()
        stored = self._keys.get((host, port))
        if stored is None:
            return None
        if stored == key_data:
            return True
        return False

    async def store(self, host: str, port: int, key: paramiko.PKey):
        self._keys[(host, port)] = base64.b64encode(key.asbytes()).decode()

    async def disconnect(self):
        pass


class TestKnownHostsPolicy:
    def test_first_visit_rejects(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key = paramiko.RSAKey.generate(2048)
        with pytest.raises(paramiko.SSHException, match="Unknown host"):
            policy.missing_host_key(None, hostname, key)
        assert store._keys.get((hostname, 22)) is None

    def test_known_key_passes(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key = paramiko.RSAKey.generate(2048)
        store._keys[(hostname, 22)] = base64.b64encode(key.asbytes()).decode()
        policy.missing_host_key(None, hostname, key)

    def test_changed_key_rejected(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key1 = paramiko.RSAKey.generate(2048)
        key2 = paramiko.RSAKey.generate(2048)
        store._keys[(hostname, 22)] = base64.b64encode(key1.asbytes()).decode()
        with pytest.raises(paramiko.SSHException, match="changed"):
            policy.missing_host_key(None, hostname, key2)
        assert base64.b64encode(key1.asbytes()).decode() == store._keys[(hostname, 22)]

    @pytest.mark.asyncio
    async def test_async_context_rejects_unknown_host(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key = paramiko.RSAKey.generate(2048)
        with pytest.raises(paramiko.SSHException, match="Unknown host"):
            await asyncio.to_thread(policy.missing_host_key, None, hostname, key)
        assert store._keys.get((hostname, 22)) is None
