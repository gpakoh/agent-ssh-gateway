"""Tests for host key store backends."""

import hashlib
import os
import tempfile

import paramiko
import pytest

from app.known_hosts import (
    FileHostKeyStore,
    HostKeyStore,
    NullHostKeyStore,
    PostgresHostKeyStore,
    classify_ssh_trust_error,
)


class TestHostKeyStoreAbc:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            HostKeyStore()  # noqa


class TestNullHostKeyStore:
    @pytest.mark.asyncio
    async def test_check_returns_none(self):
        store = NullHostKeyStore()
        result = await store.check("host", 22, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_store_is_noop(self):
        store = NullHostKeyStore()
        await store.store("host", 22, None)  # should not raise

    @pytest.mark.asyncio
    async def test_get_host_returns_none(self):
        store = NullHostKeyStore()
        result = await store.get_host("host", 22)
        assert result is None


class TestFileHostKeyStore:
    def _make_store(self):
        fd, path = tempfile.mkstemp(suffix=".tmp")
        os.close(fd)
        os.unlink(path)
        return FileHostKeyStore(path), path

    @pytest.mark.asyncio
    async def test_unknown_host_returns_none(self):
        store, _ = self._make_store()
        result = await store.check("10.0.0.1", 22, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_store_and_check_match(self):
        store, path = self._make_store()
        try:
            key = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key)
            result = await store.check("10.0.0.1", 22, key)
            assert result is True
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @pytest.mark.asyncio
    async def test_changed_key_returns_false(self):
        store, path = self._make_store()
        try:
            key1 = paramiko.RSAKey.generate(2048)
            key2 = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key1)
            result = await store.check("10.0.0.1", 22, key2)
            assert result is False
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @pytest.mark.asyncio
    async def test_list_keys_returns_entries(self):
        store, path = self._make_store()
        try:
            key = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key)
            keys = await store.list_keys()
            assert len(keys) == 1
            assert keys[0]["host"] == "10.0.0.1"
            assert keys[0]["key_type"] == key.get_name()
            assert keys[0]["fingerprint"] == hashlib.sha256(key.asbytes()).hexdigest()
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @pytest.mark.asyncio
    async def test_delete_host_removes_entries(self):
        store, path = self._make_store()
        try:
            key = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key)
            count = await store.delete_host("10.0.0.1")
            assert count == 1
            keys = await store.list_keys()
            assert len(keys) == 0
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @pytest.mark.asyncio
    async def test_delete_all_clears_everything(self):
        store, path = self._make_store()
        try:
            key1 = paramiko.RSAKey.generate(2048)
            key2 = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key1)
            await store.store("10.0.0.2", 22, key2)
            count = await store.delete_all()
            assert count == 2
            keys = await store.list_keys()
            assert len(keys) == 0
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @pytest.mark.asyncio
    async def test_list_keys_empty_returns_empty_list(self):
        store, path = self._make_store()
        try:
            keys = await store.list_keys()
            assert keys == []
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @pytest.mark.asyncio
    async def test_get_host_returns_entry(self):
        store, path = self._make_store()
        try:
            key = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key)
            entry = await store.get_host("10.0.0.1", 22)
            assert entry is not None
            assert entry["host"] == "10.0.0.1"
            assert entry["port"] == 22
            assert entry["key_type"] == key.get_name()
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @pytest.mark.asyncio
    async def test_get_host_returns_none_for_unknown(self):
        store, path = self._make_store()
        try:
            entry = await store.get_host("nobody", 22)
            assert entry is None
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @pytest.mark.asyncio
    async def test_delete_host_by_port(self):
        store, path = self._make_store()
        try:
            key = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key)
            # For bare hostnames, FileHostKeyStore removes all entries matching host
            count = await store.delete_host("10.0.0.1", 22)
            assert count >= 1
            remaining = await store.list_keys()
            assert len(remaining) == 0
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


class TestClassifyTrustError:
    def test_unknown_pattern(self):
        assert classify_ssh_trust_error("Unknown host 10.0.0.1:22") == "unknown"
        assert classify_ssh_trust_error("unknown host some.host.com") == "unknown"

    def test_changed_pattern(self):
        msg = "Host key for 10.0.0.1:22 changed — possible MITM attack"
        assert classify_ssh_trust_error(msg) == "changed"

    def test_non_trust_error(self):
        assert classify_ssh_trust_error("Connection refused") is None
        assert classify_ssh_trust_error("Authentication failed") is None
        assert classify_ssh_trust_error("") is None


class TestPostgresHostKeyStore:
    @pytest.mark.asyncio
    async def test_unknown_host_returns_none(self):
        store = PostgresHostKeyStore("sqlite+aiosqlite:///:memory:")
        await store._init_db()
        result = await store.check("10.0.0.1", 22, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_store_and_check_match(self):
        store = PostgresHostKeyStore("sqlite+aiosqlite:///:memory:")
        await store._init_db()
        key = paramiko.RSAKey.generate(2048)
        await store.store("10.0.0.1", 22, key)
        result = await store.check("10.0.0.1", 22, key)
        assert result is True

    @pytest.mark.asyncio
    async def test_changed_key_returns_false(self):
        store = PostgresHostKeyStore("sqlite+aiosqlite:///:memory:")
        await store._init_db()
        key1 = paramiko.RSAKey.generate(2048)
        key2 = paramiko.RSAKey.generate(2048)
        await store.store("10.0.0.1", 22, key1)
        result = await store.check("10.0.0.1", 22, key2)
        assert result is False

    @pytest.mark.asyncio
    async def test_pg_get_host_returns_entry(self):
        store = PostgresHostKeyStore("sqlite+aiosqlite:///:memory:")
        await store._init_db()
        key = paramiko.RSAKey.generate(2048)
        await store.store("10.0.0.1", 22, key)
        entry = await store.get_host("10.0.0.1", 22)
        assert entry is not None
        assert entry["host"] == "10.0.0.1"
        assert entry["port"] == 22

    @pytest.mark.asyncio
    async def test_pg_delete_host_by_port(self):
        store = PostgresHostKeyStore("sqlite+aiosqlite:///:memory:")
        await store._init_db()
        key1 = paramiko.RSAKey.generate(2048)
        key2 = paramiko.RSAKey.generate(2048)
        await store.store("multi", 22, key1)
        await store.store("multi", 2222, key2)
        count = await store.delete_host("multi", 22)
        assert count == 1
        entry22 = await store.get_host("multi", 22)
        assert entry22 is None
        entry2222 = await store.get_host("multi", 2222)
        assert entry2222 is not None
