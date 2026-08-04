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

    def test_non_default_port_key_matches_bare_host_trust(self):
        """Paramiko passes "[host]:port" for non-22 ports (see SSHClient.connect's
        server_hostkey_name); the known-hosts API stores by bare host + explicit
        port. Without normalizing paramiko's bracket notation first, a host trusted
        on a non-default port would be permanently "unknown" at connect time."""
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=2222)
        key = paramiko.RSAKey.generate(2048)
        # Simulates trusting the host via the /api/known-hosts endpoint, which
        # always stores (bare_host, port) — never a bracketed string.
        store._keys[("sshd.internal", 2222)] = base64.b64encode(key.asbytes()).decode()

        # Simulates what paramiko actually passes for a non-default port.
        policy.missing_host_key(None, "[sshd.internal]:2222", key)

    def test_non_default_port_unknown_host_still_rejects(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=2222)
        key = paramiko.RSAKey.generate(2048)
        with pytest.raises(paramiko.SSHException, match="Unknown host sshd.internal:2222"):
            policy.missing_host_key(None, "[sshd.internal]:2222", key)
