"""Integration tests: GatewayOAuthProvider + ClientStore.

Regression coverage for the "Client ID ... not found" bug: dynamically
registered OAuth clients (RFC 7591) lived only in
GatewayOAuthProvider._clients, an in-memory dict -- every process
restart silently forgot every client a connector had ever registered,
so the next reconnection attempt failed even though nothing about the
connection itself had changed.
"""

import asyncio
import json
import os
import re
import tempfile
from dataclasses import asdict

import pytest

from examples.mcp_server.client_store import ClientStore
from examples.mcp_server.oauth_provider import GatewayOAuthProvider, StoredClient

pytestmark = pytest.mark.integration


@pytest.fixture
def store_path():
    # First boot is a missing store, not a present zero-byte (corrupt) store.
    with tempfile.TemporaryDirectory(prefix="mcp-client-store-") as directory:
        yield os.path.join(directory, "clients.json")


def _client_info(client_name: str = "test-connector"):
    class _Info:
        redirect_uris = ["https://chat.example.com/callback"]
        client_name = "test-connector"
        token_endpoint_auth_method = "none"
        scope = "mcp:read mcp:project"

    info = _Info()
    info.client_name = client_name
    return info


def test_set_client_store(store_path):
    provider = GatewayOAuthProvider()
    store = ClientStore(store_path)
    provider.set_client_store(store)
    assert provider._client_store is store


def test_load_clients_no_store():
    provider = GatewayOAuthProvider()
    assert provider.load_clients() == 0


def test_load_clients_missing_store(store_path):
    provider = GatewayOAuthProvider()
    provider.set_client_store(ClientStore(store_path))
    assert provider.load_clients() == 0


def test_register_client_persists_to_store(store_path):
    """register_client() (the DCR handler) must persist immediately, not
    just hold the registration in memory.
    """
    provider = GatewayOAuthProvider()
    provider.set_client_store(ClientStore(store_path))

    info = _client_info()
    asyncio.run(provider.register_client(info))
    client_id = info.client_id

    # A second, freshly constructed store reading the same path must see it.
    store2 = ClientStore(store_path)
    loaded = store2.load()
    assert len(loaded) == 1
    assert loaded[0].client_id == client_id
    assert loaded[0].redirect_uris == ["https://chat.example.com/callback"]


def test_dcr_handler_phase_trace_separates_dispatch_and_store_without_client_data(
    store_path, capsys
):
    provider = GatewayOAuthProvider()
    provider.set_client_store(ClientStore(store_path))
    info = _client_info("private-client-name-do-not-log")
    info.redirect_uris = ["https://example.invalid/?secret=private"]
    asyncio.run(provider.register_client(info))
    lines = capsys.readouterr().err.splitlines()
    handler = [line for line in lines if line.startswith("DCR_HANDLER_TRACE ")]
    assert len(handler) == 4
    phases = ("entered", "store_submit", "store_done", "registered")
    ids = set()
    for line, phase in zip(handler, phases, strict=True):
        match = re.fullmatch(
            rf"DCR_HANDLER_TRACE id=([0-9a-f]{{12}}) phase={phase} elapsed_ms=([0-9]{{1,7}})",
            line,
        )
        assert match is not None
        ids.add(match.group(1))
    assert len(ids) == 1
    assert all("private" not in line and store_path not in line for line in lines)
    assert len(provider.list_clients()) == 1


def test_client_store_phase_trace_never_emits_client_data_or_paths(store_path, capsys):
    """A registration's bounded store phases are safe to retain in CI."""
    store = ClientStore(store_path)
    client = StoredClient(
        client_id="mcp_client_secret-value-should-not-log",
        redirect_uris=["https://callback.example.com/?token=secret-should-not-log"],
    )
    store.add(client)
    lines = capsys.readouterr().err.splitlines()

    assert len(lines) == 4
    expected_phases = ("begin", "lock_acquired", "loaded", "persisted")
    ids = set()
    for line, phase in zip(lines, expected_phases, strict=True):
        match = re.fullmatch(
            rf"DCR_STORE_TRACE id=([0-9a-f]{{12}}) phase={phase} elapsed_ms=([0-9]{{1,7}})",
            line,
        )
        assert match is not None
        ids.add(match.group(1))
    assert len(ids) == 1
    assert all("secret" not in line and store_path not in line for line in lines)
    assert len(store.load()) == 1


def test_failed_store_phase_trace_is_sanitized_and_preserves_error(
    store_path, monkeypatch, capsys
):
    store = ClientStore(store_path)

    def fail_persist(_clients):
        raise PermissionError("client_secret=never-print-this-or-store-path " + store_path)

    monkeypatch.setattr(store, "_write", fail_persist)
    with pytest.raises(PermissionError, match="client_secret="):
        store.add(StoredClient(client_id="test-failed", redirect_uris=["https://a.example/cb"]))
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 4
    assert [re.search(r" phase=([a-z_]+) ", line).group(1) for line in lines] == [
        "begin", "lock_acquired", "loaded", "failed"
    ]
    assert all(
        re.fullmatch(
            r"DCR_STORE_TRACE id=[0-9a-f]{12} phase=[a-z_]+ elapsed_ms=[0-9]{1,7}",
            line,
        )
        for line in lines
    )
    assert "never-print-this" not in "\n".join(lines)
    assert store_path not in "\n".join(lines)
    assert store.load() == []


def test_client_store_trace_sink_failure_does_not_break_persistence(
    store_path, monkeypatch
):
    """Best-effort phase logging cannot change the durable store contract."""
    from examples.mcp_server import client_store as store_module

    def broken_sink(*_args, **_kwargs):
        raise OSError("stderr unavailable")

    monkeypatch.setattr(store_module, "print", broken_sink, raising=False)
    store = ClientStore(store_path)
    store.add(StoredClient(client_id="test-trace-sink", redirect_uris=["https://example.com/cb"]))
    assert len(store.load()) == 1


def test_client_store_lock_contention_fails_closed_and_recovers(
    store_path, monkeypatch
):
    """A stuck writer cannot hold registration indefinitely or corrupt data."""
    import fcntl
    import time

    from examples.mcp_server import client_store as store_module

    monkeypatch.setattr(store_module, "CLIENT_STORE_LOCK_TIMEOUT_SECONDS", 0.15)
    store = ClientStore(store_path)
    client = StoredClient(client_id="mcp_client_busy", redirect_uris=["https://example.com/cb"])
    with open(store_path + ".lock", "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        began = time.monotonic()
        with pytest.raises(store_module.ClientStoreBusyError, match="temporarily busy"):
            store.add(client)
        assert time.monotonic() - began < 2
        assert store.load() == []
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)

    store.add(client)
    assert len(store.load()) == 1
    assert store.load()[0].client_id == "mcp_client_busy"


@pytest.mark.asyncio
async def test_register_client_store_write_does_not_block_oauth_event_loop(
    store_path,
):
    """A stalled filesystem writer must not prevent another OAuth coroutine."""
    import threading

    class DelayedStore(ClientStore):
        def __init__(self, path):
            super().__init__(path)
            self.entered = threading.Event()
            self.release = threading.Event()

        def add(self, client):
            self.entered.set()
            self.release.wait(timeout=4)
            super().add(client)

    store = DelayedStore(store_path)
    provider = GatewayOAuthProvider()
    provider.set_client_store(store)
    info = _client_info("concurrent-oauth")
    task = asyncio.create_task(provider.register_client(info))
    try:
        # With the old synchronous call in the event loop, add() would block
        # this coroutine until its 4-second emergency release elapsed.
        assert await asyncio.wait_for(asyncio.to_thread(store.entered.wait, 2), 3)
        assert not task.done()
        assert "client_id" not in vars(info)
        await asyncio.wait_for(asyncio.sleep(0), 1)
    finally:
        store.release.set()
    await asyncio.wait_for(task, 4)
    assert await provider.get_client(info.client_id) is not None
    assert len(ClientStore(store_path).load()) == 1


@pytest.mark.asyncio
async def test_failed_client_store_write_does_not_publish_ephemeral_client(
    store_path, monkeypatch
):
    from examples.mcp_server.client_store import ClientStoreBusyError

    store = ClientStore(store_path)

    def refused(_client):
        raise ClientStoreBusyError("OAuth client store temporarily busy")

    monkeypatch.setattr(store, "add", refused)
    provider = GatewayOAuthProvider()
    provider.set_client_store(store)
    info = _client_info("failed-oauth")
    with pytest.raises(ClientStoreBusyError):
        await provider.register_client(info)
    assert not provider._clients
    assert "client_id" not in vars(info)


def test_client_survives_provider_restart(store_path):
    """The actual bug scenario: register a client on one provider
    instance, then simulate a process restart (a brand-new provider +
    store instance reading the same file) and confirm the client is
    still known -- this is exactly what a systemd/container restart
    does between a connector's registration and its next reconnection.
    """
    provider1 = GatewayOAuthProvider()
    provider1.set_client_store(ClientStore(store_path))
    info = _client_info()
    asyncio.run(provider1.register_client(info))
    client_id = info.client_id

    # Simulate restart: brand-new provider, brand-new store, same file.
    provider2 = GatewayOAuthProvider()
    provider2.set_client_store(ClientStore(store_path))
    loaded_count = provider2.load_clients()

    assert loaded_count == 1
    found = asyncio.run(provider2.get_client(client_id))
    assert found is not None
    assert found.client_id == client_id


def test_load_clients_multiple(store_path):
    store = ClientStore(store_path)
    store.add(StoredClient(client_id="mcp_client_a", redirect_uris=["https://a.example.com/cb"]))
    store.add(StoredClient(client_id="mcp_client_b", redirect_uris=["https://b.example.com/cb"]))

    provider = GatewayOAuthProvider()
    provider.set_client_store(store)
    assert provider.load_clients() == 2
    assert set(provider._clients.keys()) == {"mcp_client_a", "mcp_client_b"}


def test_add_same_client_id_replaces_not_duplicates(store_path):
    store = ClientStore(store_path)
    store.add(StoredClient(client_id="mcp_client_x", redirect_uris=["https://old.example.com/cb"]))
    store.add(StoredClient(client_id="mcp_client_x", redirect_uris=["https://new.example.com/cb"]))

    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].redirect_uris == ["https://new.example.com/cb"]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{\"version\":1,\"clients\":[",
        "null",
        "{}",
        '{"version":1,"clients":{}}',
        '{"version":2,"clients":[]}',
        '{"version":1,"clients":[{"redirect_uris":[]}]}',
    ],
)
def test_existing_invalid_client_store_fails_closed_without_erasing_bytes(
    tmp_path, raw, capsys
):
    path = tmp_path / "clients.json"
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)
    store = ClientStore(str(path))
    with pytest.raises(ValueError, match="OAuth client store"):
        store.load()
    with pytest.raises(ValueError, match="OAuth client store"):
        store.add(StoredClient(client_id="new-client", redirect_uris=["https://example.com/cb"]))
    assert path.read_text(encoding="utf-8") == raw
    assert list(tmp_path.glob(".mcp_clients_*.tmp")) == []
    assert "new-client" not in capsys.readouterr().err


def test_corrupt_client_store_does_not_silently_load_empty_provider(tmp_path):
    path = tmp_path / "clients.json"
    path.write_text('{"version":1,"clients":', encoding="utf-8")
    path.chmod(0o600)
    provider = GatewayOAuthProvider()
    provider.set_client_store(ClientStore(str(path)))
    with pytest.raises(ValueError, match="OAuth client store"):
        provider.load_clients()


def test_client_store_fsync_error_preserves_previous_data_without_orphan_temp(
    tmp_path, monkeypatch
):
    from examples.mcp_server import client_store as module

    path = tmp_path / "clients.json"
    store = ClientStore(str(path))
    store.add(StoredClient(client_id="existing", redirect_uris=["https://example.com/old"]))
    original = path.read_bytes()

    def fail_fsync(_fd):
        raise OSError("injected fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fsync", fail_fsync)
        with pytest.raises(OSError, match="injected fsync"):
            store.add(StoredClient(client_id="new", redirect_uris=["https://example.com/new"]))

    assert path.read_bytes() == original
    assert [client.client_id for client in store.load()] == ["existing"]
    assert list(tmp_path.glob(".mcp_clients_*.tmp")) == []


def test_client_store_short_writes_produce_complete_json(tmp_path, monkeypatch):
    from examples.mcp_server import client_store as module

    store = ClientStore(str(tmp_path / "clients.json"))
    original_write = module.os.write

    def short_write(fd, data):
        return original_write(fd, data[: max(1, len(data) // 4)])

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "write", short_write)
        store.add(StoredClient(client_id="short-write", redirect_uris=["https://example.com/cb"]))

    assert [client.client_id for client in store.load()] == ["short-write"]


def test_client_store_zero_byte_write_is_error_not_empty_published_file(
    tmp_path, monkeypatch
):
    from examples.mcp_server import client_store as module

    path = tmp_path / "clients.json"
    store = ClientStore(str(path))
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "write", lambda _fd, _data: 0)
        with pytest.raises(OSError, match="client store write"):
            store.add(StoredClient(client_id="new", redirect_uris=["https://example.com/cb"]))

    assert not path.exists()
    assert list(tmp_path.glob(".mcp_clients_*.tmp")) == []


def test_post_replace_ack_loss_is_reconciled_before_client_retry(tmp_path, monkeypatch):
    """An error after successful rename is UNKNOWN until exact durable readback."""
    from examples.mcp_server import client_store as module

    path = tmp_path / "clients.json"
    store = ClientStore(str(path))
    provider = GatewayOAuthProvider()
    provider.set_client_store(store)
    info = _client_info("ack-lost")
    real_replace = module.os.replace
    simulated = False

    def replace_then_lose_response(source, target):
        nonlocal simulated
        real_replace(source, target)
        if not simulated:
            simulated = True
            raise TimeoutError("reply lost after commit")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", replace_then_lose_response)
        asyncio.run(provider.register_client(info))
    assert simulated
    assert "client_id" in vars(info)
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".mcp_clients_*.tmp")) == []

    # Restart reveals exactly one durable registration, no blind re-registration.
    fresh = GatewayOAuthProvider()
    fresh.set_client_store(ClientStore(str(path)))
    assert fresh.load_clients() == 1
    assert asyncio.run(fresh.get_client(info.client_id)) is not None
    store.add(store.load()[0])  # retry the SAME known identity; no duplicate
    assert [c.client_id for c in store.load()] == [info.client_id]


def test_pre_replace_timeout_requires_readback_before_explicit_retry(
    tmp_path, monkeypatch
):
    from examples.mcp_server import client_store as module

    path = tmp_path / "clients.json"
    store = ClientStore(str(path))
    old = StoredClient(client_id="existing", redirect_uris=["https://example.com/old"])
    new = StoredClient(client_id="new", redirect_uris=["https://example.com/new"])
    store.add(old)
    before = path.read_bytes()

    def timeout_before_commit(_source, _target):
        raise TimeoutError("no rename occurred")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", timeout_before_commit)
        with pytest.raises(TimeoutError, match="no rename occurred"):
            store.add(new)

    # The actual durable state is authoritative; a failure message alone isn't.
    assert path.read_bytes() == before
    assert [c.client_id for c in store.load()] == ["existing"]
    assert list(tmp_path.glob(".mcp_clients_*.tmp")) == []
    store.add(new)  # deliberate retry only after checking the first outcome
    restarted = ClientStore(str(path))
    assert [c.client_id for c in restarted.load()] == ["existing", "new"]


def test_ambiguous_post_replace_mismatched_data_is_not_reported_as_success(
    tmp_path, monkeypatch
):
    from examples.mcp_server import client_store as module

    path = tmp_path / "clients.json"
    store = ClientStore(str(path))
    new = StoredClient(client_id="wanted", redirect_uris=["https://example.com/wanted"])
    different = StoredClient(client_id="other", redirect_uris=["https://example.com/other"])
    real_replace = module.os.replace

    def commit_different_then_lose_ack(source, target):
        real_replace(source, target)
        # Simulate an external writer outside our lock breaking exact identity.
        path.write_text(
            json.dumps({"version": 1, "clients": [asdict(different)]}),
            encoding="utf-8",
        )
        raise TimeoutError("ambiguous completion and nonmatching data")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", commit_different_then_lose_ack)
        with pytest.raises(TimeoutError, match="ambiguous"):
            store.add(new)

    assert [client.client_id for client in store.load()] == ["other"]
    assert list(tmp_path.glob(".mcp_clients_*.tmp")) == []


def test_partial_result_after_write_error_keeps_previous_state(tmp_path, monkeypatch):
    from examples.mcp_server import client_store as module

    path = tmp_path / "clients.json"
    store = ClientStore(str(path))
    store.add(StoredClient(client_id="before", redirect_uris=["https://example.com/b"]))
    old_bytes = path.read_bytes()
    real_write = module.os.write
    wrote_partial = False

    def partial_then_error(fd, data):
        nonlocal wrote_partial
        if not wrote_partial:
            wrote_partial = True
            return real_write(fd, data[:12])
        raise OSError("injected partial-result failure")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "write", partial_then_error)
        with pytest.raises(OSError, match="partial-result"):
            store.add(StoredClient(client_id="after", redirect_uris=["https://example.com/a"]))
    assert wrote_partial
    assert path.read_bytes() == old_bytes
    assert [c.client_id for c in ClientStore(str(path)).load()] == ["before"]
    assert list(tmp_path.glob(".mcp_clients_*.tmp")) == []

