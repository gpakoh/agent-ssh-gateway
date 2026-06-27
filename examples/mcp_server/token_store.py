"""Persistent token store for MCP tokens.

Stores hashed token entries in a JSON file with atomic writes and
fcntl-based file locking.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class StoredTokenEntry:
    """A persisted token entry (hash, never raw token)."""

    id: str
    token_hash: str
    name: str
    profile: str
    scopes: list[str]
    created_at: str
    expires_at: str | None = None
    revoked_at: str | None = None
    last_used_at: str | None = None


def _default_store_path() -> str:
    return os.environ.get(
        "MCP_TOKEN_STORE_FILE",
        "/var/lib/agent-ssh-gateway/mcp_tokens.json",
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
            raise PermissionError(f"Token store file is world-writable: {path_str}")
    except FileNotFoundError:
        pass


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _entry_to_dict(e: StoredTokenEntry) -> dict[str, Any]:
    d = asdict(e)
    return {k: v for k, v in d.items() if v is not None}


def _dict_to_entry(d: dict[str, Any]) -> StoredTokenEntry:
    return StoredTokenEntry(
        id=d["id"],
        token_hash=d["token_hash"],
        name=d["name"],
        profile=d["profile"],
        scopes=d["scopes"],
        created_at=d["created_at"],
        expires_at=d.get("expires_at"),
        revoked_at=d.get("revoked_at"),
        last_used_at=d.get("last_used_at"),
    )


class TokenStore:
    """Persistent token store with atomic file writes.

    Uses a JSON file as the backing store. All writes go through a
    tempfile + os.replace dance and are serialised via fcntl.flock on
    a companion ``.lock`` file to prevent corruption under concurrent
    processes.
    """

    def __init__(self, store_path: str | None = None) -> None:
        self._path = store_path or _default_store_path()
        self._lock_path = self._path + ".lock"
        _ensure_parent(self._path)
        _check_not_world_writable(self._path)

    def load(self) -> list[StoredTokenEntry]:
        """Load all token entries from the store file."""
        try:
            with open(self._path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except json.JSONDecodeError:
            return []
        entries = data.get("tokens", [])
        return [_dict_to_entry(e) for e in entries]

    def add(self, entry: StoredTokenEntry) -> None:
        """Append an entry and persist."""
        entries = self.load()
        entries.append(entry)
        self._save(entries)

    def revoke(self, token_id: str) -> StoredTokenEntry | None:
        """Mark a token as revoked by id. Returns the entry or None."""
        entries = self.load()
        for e in entries:
            if e.id == token_id and e.revoked_at is None:
                e.revoked_at = _iso_now()
                self._save(entries)
                return e
        return None

    def find_by_hash(self, token_hash: str) -> StoredTokenEntry | None:
        """Find an entry by its hash (exact match)."""
        for e in self.load():
            if e.token_hash == token_hash:
                return e
        return None

    def _save(self, entries: list[StoredTokenEntry]) -> None:
        """Atomically write entries to the store file.

        Uses a tempfile in the same directory + os.replace for atomic
        replacement, guarded by fcntl.flock on a .lock file.
        """
        payload: dict[str, Any] = {
            "version": 1,
            "tokens": [_entry_to_dict(e) for e in entries],
        }
        raw = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

        with open(self._lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                fd, tmp = tempfile.mkstemp(
                    dir=os.path.dirname(self._path) or ".",
                    prefix=".mcp_tokens_",
                    suffix=".tmp",
                )
                try:
                    os.write(fd, raw.encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, self._path)
                os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
