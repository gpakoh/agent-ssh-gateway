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
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from examples.mcp_server.oauth_provider import StoredClient


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
        """Load all registered clients from the store file."""
        try:
            with open(self._path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except json.JSONDecodeError:
            return []
        return [StoredClient(**c) for c in data.get("clients", [])]

    def add(self, client: StoredClient) -> None:
        """Persist a newly registered client (locked read-modify-write)."""
        with self._locked():
            clients = [c for c in self.load() if c.client_id != client.client_id]
            clients.append(client)
            self._write(clients)

    def _locked(self) -> _LockedStore:
        """Acquire an exclusive lock over the whole read-modify-write cycle."""
        return _LockedStore(self)

    def _write(self, clients: list[StoredClient]) -> None:
        """Atomically write clients. Caller must hold the store lock."""
        payload: dict[str, Any] = {
            "version": 1,
            "clients": [asdict(c) for c in clients],
        }
        raw = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(self._path) or ".",
            prefix=".mcp_clients_",
            suffix=".tmp",
        )
        try:
            os.write(fd, raw.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._path)
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)


class _LockedStore:
    """Context manager holding an exclusive flock on the store lock file.

    Ensures ``add`` is atomic across processes.
    """

    def __init__(self, store: ClientStore) -> None:
        self._store = store

    def __enter__(self) -> _LockedStore:
        self._lf = open(self._store._lock_path, "w")
        fcntl.flock(self._lf.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            fcntl.flock(self._lf.fileno(), fcntl.LOCK_UN)
        finally:
            self._lf.close()
