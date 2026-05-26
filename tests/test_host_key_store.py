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
    create_host_key_store,
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


class TestFileHostKeyStore:
    @pytest.mark.asyncio
    async def test_unknown_host_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp") as f:
            os.unlink(f.name)
            store = FileHostKeyStore(f.name)
            result = await store.check("10.0.0.1", 22, None)
            assert result is None

    @pytest.mark.asyncio
    async def test_store_and_check_match(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp") as f:
            os.unlink(f.name)
            store = FileHostKeyStore(f.name)
            key = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key)
            result = await store.check("10.0.0.1", 22, key)
            assert result is True

    @pytest.mark.asyncio
    async def test_changed_key_returns_false(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp") as f:
            os.unlink(f.name)
            store = FileHostKeyStore(f.name)
            key1 = paramiko.RSAKey.generate(2048)
            key2 = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key1)
            result = await store.check("10.0.0.1", 22, key2)
            assert result is False

    @pytest.mark.asyncio
    async def test_list_keys_returns_entries(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp") as f:
            os.unlink(f.name)
            store = FileHostKeyStore(f.name)
            key = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key)
            keys = await store.list_keys()
            assert len(keys) == 1
            assert keys[0]["host"] == "10.0.0.1"
            assert keys[0]["key_type"] == key.get_name()
            assert keys[0]["fingerprint"] == hashlib.sha256(key.asbytes()).hexdigest()

    @pytest.mark.asyncio
    async def test_delete_host_removes_entries(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp") as f:
            os.unlink(f.name)
            store = FileHostKeyStore(f.name)
            key = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key)
            count = await store.delete_host("10.0.0.1")
            assert count == 1
            keys = await store.list_keys()
            assert len(keys) == 0

    @pytest.mark.asyncio
    async def test_delete_all_clears_everything(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp") as f:
            os.unlink(f.name)
            store = FileHostKeyStore(f.name)
            key1 = paramiko.RSAKey.generate(2048)
            key2 = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key1)
            await store.store("10.0.0.2", 22, key2)
            count = await store.delete_all()
            assert count == 2
            keys = await store.list_keys()
            assert len(keys) == 0

    @pytest.mark.asyncio
    async def test_list_keys_empty_returns_empty_list(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp") as f:
            os.unlink(f.name)
            store = FileHostKeyStore(f.name)
            keys = await store.list_keys()
            assert keys == []


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
