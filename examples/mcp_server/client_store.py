"""Persistent client store for MCP dynamically-registered OAuth clients.

Dynamically registered clients (RFC 7591) previously lived only in
GatewayOAuthProvider._clients, an in-memory dict -- every process restart
(including the systemd MCP service's mandatory restart-after-every-code-
change cycle) silently forgot every client a connector had ever
registered, producing "Client ID ... not found" on the connector's next
reconnection attempt. Persisted the same way token_store.py's TokenStore
already persists tokens: a JSON file with atomic tempfile+os.replace
writes, serialised via fcntl.flock on a companion .lock file.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from examples.mcp_server.oauth_provider import StoredClient

CLIENT_STORE_LOCK_TIMEOUT_SECONDS = 5.0
CLIENT_STORE_LOCK_POLL_SECONDS = 0.05
_DCR_STORE_PHASES = frozenset({"begin", "lock_acquired", "loaded", "persisted", "failed"})


def _trace_client_store_phase(trace_id: str, phase: str, started: float) -> None:
    """Emit bounded, credential-free client-store latency phases.

    Never include the client ID, redirect URI, store path, exception or
    serialized JSON. The fixed shape is safe to capture on failed host-smoke.
    """
    if phase not in _DCR_STORE_PHASES:
        raise ValueError("unknown DCR store phase")
    if len(trace_id) != 12 or any(c not in "0123456789abcdef" for c in trace_id):
        raise ValueError("invalid DCR store trace id")
    elapsed_ms = min(9_999_999, max(0, int((time.monotonic() - started) * 1000)))
    try:
        print(
            f"DCR_STORE_TRACE id={trace_id} phase={phase} elapsed_ms={elapsed_ms}",
            file=sys.stderr,
            flush=True,
        )
    except OSError:
        # Diagnostic stderr must never make OAuth registration fail.
        pass


class ClientStoreBusyError(TimeoutError):
    """Durable OAuth client registration cannot acquire its store lock."""


class ClientStoreCorruptError(ValueError):
    """Existing OAuth registrations cannot be read safely; never replace them."""


def _default_store_path() -> str:
    return os.environ.get(
        "MCP_CLIENT_STORE_FILE",
        "/var/lib/agent-ssh-gateway/mcp_clients.json",
    )


def _ensure_parent(path_str: str) -> None:
    parent = os.path.dirname(path_str)
    if not parent:
        return
    Path(parent).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, stat.S_IRWXU)
    except PermissionError:
        pass  # Existing system dir (e.g., /tmp) is fine


def _check_not_world_writable(path_str: str) -> None:
    try:
        st = os.stat(path_str)
        if st.st_mode & stat.S_IWOTH:
            raise PermissionError(f"Client store file is world-writable: {path_str}")
    except FileNotFoundError:
        pass


class ClientStore:
    """Persistent OAuth dynamic-client-registration store.

    Uses a JSON file as the backing store. All writes go through a
    tempfile + os.replace dance and are serialised via fcntl.flock on a
    companion ``.lock`` file to prevent corruption under concurrent
    processes -- same pattern as token_store.py's TokenStore.
    """

    def __init__(self, store_path: str | None = None) -> None:
        self._path = store_path or _default_store_path()
        self._lock_path = self._path + ".lock"
        _ensure_parent(self._path)
        _check_not_world_writable(self._path)

    def load(self) -> list[StoredClient]:
        """Load existing registrations; only a missing store is empty.

        A present but truncated or invalid store must never be mistaken for
        an empty store: the next DCR write would otherwise erase old clients.
        """
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ClientStoreCorruptError("OAuth client store has invalid data") from None

        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
            or not isinstance(data.get("clients"), list)
        ):
            raise ClientStoreCorruptError("OAuth client store has invalid data")

        entries = data["clients"]
        if any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("client_id"), str)
            or not entry["client_id"]
            or not isinstance(entry.get("redirect_uris"), list)
            or not all(isinstance(uri, str) and uri for uri in entry["redirect_uris"])
            for entry in entries
        ):
            raise ClientStoreCorruptError("OAuth client store has invalid data")
        try:
            return [StoredClient(**entry) for entry in entries]
        except (TypeError, ValueError):
            raise ClientStoreCorruptError("OAuth client store has invalid data") from None

    def add(self, client: StoredClient) -> None:
        """Persist a new client, leaving fixed-shape evidence of blocking stages."""
        trace_id = secrets.token_hex(6)
        started = time.monotonic()
        _trace_client_store_phase(trace_id, "begin", started)
        try:
            with self._locked():
                _trace_client_store_phase(trace_id, "lock_acquired", started)
                clients = [c for c in self.load() if c.client_id != client.client_id]
                _trace_client_store_phase(trace_id, "loaded", started)
                clients.append(client)
                try:
                    self._write(clients)
                except OSError:
                    # A timeout / lost acknowledgment from the last write
                    # operation may arrive AFTER os.replace committed. Under
                    # this lock, reconcile exact durable bytes before deciding
                    # whether the registration failed. Never blindly retry.
                    if not self._matches_committed(clients):
                        raise
                _trace_client_store_phase(trace_id, "persisted", started)
        except Exception:
            # No exception text or filesystem path in public CI evidence.
            _trace_client_store_phase(trace_id, "failed", started)
            raise

    def _locked(self) -> _LockedStore:
        """Acquire an exclusive lock over the whole read-modify-write cycle."""
        return _LockedStore(self)

    @staticmethod
    def _encode_clients(clients: list[StoredClient]) -> bytes:
        payload: dict[str, Any] = {
            "version": 1,
            "clients": [asdict(c) for c in clients],
        }
        return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def _matches_committed(self, clients: list[StoredClient]) -> bool:
        """Read-only, exact-byte reconciliation of an ambiguous write result.

        Do not follow symlinks or read special files. A proof that is absent,
        truncated, or unreadable is NOT a successful commit.
        """
        expected = self._encode_clients(clients)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(self._path, flags)
            try:
                st = os.fstat(fd)
                if (
                    not stat.S_ISREG(st.st_mode)
                    or st.st_nlink != 1
                    or st.st_uid != os.geteuid()
                    or st.st_mode & 0o077
                ):
                    return False
                with os.fdopen(fd, "rb", closefd=False) as f:
                    return f.read(len(expected) + 1) == expected
            finally:
                os.close(fd)
        except OSError:
            return False

    def _write(self, clients: list[StoredClient]) -> None:
        """Atomically write clients. Caller must hold the store lock."""
        raw = self._encode_clients(clients)

        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(self._path) or ".",
            prefix=".mcp_clients_",
            suffix=".tmp",
        )
        try:
            try:
                # Set permissions before publication, not after os.replace.
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                remaining = memoryview(raw)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("OAuth client store write made no progress")
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            # Never publish a partially written or unsynced JSON file.
            os.replace(tmp, self._path)
        except BaseException:
            # Keep the previous store byte-identical on pre-rename failures;
            # do not leave credentials in abandoned temporary files.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class _LockedStore:
    """Context manager holding an exclusive flock on the store lock file.

    Ensures ``add`` is atomic across processes.
    """

    def __init__(self, store: ClientStore) -> None:
        self._store = store

    def __enter__(self) -> _LockedStore:
        self._lf = open(self._store._lock_path, "w")
        deadline = time.monotonic() + CLIENT_STORE_LOCK_TIMEOUT_SECONDS
        try:
            while True:
                try:
                    fcntl.flock(self._lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ClientStoreBusyError("OAuth client store temporarily busy") from None
                    time.sleep(min(remaining, CLIENT_STORE_LOCK_POLL_SECONDS))
        except BaseException:
            # __exit__ is not called when context-manager entry fails.
            self._lf.close()
            raise

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            fcntl.flock(self._lf.fileno(), fcntl.LOCK_UN)
        finally:
            self._lf.close()
