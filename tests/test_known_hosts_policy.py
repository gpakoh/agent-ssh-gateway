"""Tests for KnownHostsPolicy integration."""

import asyncio
import base64
import pytest
import paramiko

from app.known_hosts import KnownHostsPolicy, HostKeyStore


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


class TestKnownHostsPolicy:
    def test_first_visit_stores_key(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key = paramiko.RSAKey.generate(2048)
        policy.missing_host_key(None, hostname, key)
        assert store._keys.get((hostname, 22)) is not None

    def test_same_key_does_not_raise(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key = paramiko.RSAKey.generate(2048)
        policy.missing_host_key(None, hostname, key)  # first visit
        policy.missing_host_key(None, hostname, key)  # second visit — no error

    def test_changed_key_updates_and_returns(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key1 = paramiko.RSAKey.generate(2048)
        key2 = paramiko.RSAKey.generate(2048)
        policy.missing_host_key(None, hostname, key1)  # store key1
        policy.missing_host_key(None, hostname, key2)  # update to key2
        assert store._keys[(hostname, 22)] == base64.b64encode(key2.asbytes()).decode()

    @pytest.mark.asyncio
    async def test_async_context_uses_run_coroutine_threadsafe(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key = paramiko.RSAKey.generate(2048)
        await asyncio.to_thread(policy.missing_host_key, None, hostname, key)
        assert store._keys.get((hostname, 22)) is not None
