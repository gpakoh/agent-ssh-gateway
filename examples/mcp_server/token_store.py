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
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

TOKEN_STORE_VERSION = 2
TokenType = Literal["access", "refresh"]


@dataclass
class StoredTokenEntry:
    """A persisted token entry (hash, never raw token)."""

    id: str
    token_hash: str
    name: str
    profile: str
    scopes: list[str]
    created_at: str
    client_id: str = "mcp_static"
    type: TokenType = "access"
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
    token_type_raw = d.get("type", "access")
    if token_type_raw not in ("access", "refresh"):
        raise ValueError("Token store record has invalid token type")
    if token_type_raw == "refresh" and not d.get("client_id"):
        raise ValueError("OAuth refresh token store record is missing client_id")
    token_type = cast(TokenType, token_type_raw)
    return StoredTokenEntry(
        id=d["id"],
        token_hash=d["token_hash"],
        name=d["name"],
        profile=d["profile"],
        scopes=d["scopes"],
        created_at=d["created_at"],
        client_id=d.get("client_id", "mcp_static"),
        type=token_type,
        expires_at=d.get("expires_at"),
        revoked_at=d.get("revoked_at"),
        last_used_at=d.get("last_used_at"),
    )


def _parse_expiry_epoch(expires_at: str | None) -> float | None:
    """Parse a persisted ``expires_at`` to epoch seconds.

    Returns ``None`` for a missing value (no expiry). An unparseable
    value returns ``0.0`` so callers fail closed and treat the record
    as already expired rather than granting open-ended validity.
    """
    if expires_at is None:
        return None
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return expires.timestamp()
    except (ValueError, OSError):
        return 0.0


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
        _check_not_world_writable(self._path)

    def prepare_durable_storage(self) -> None:
        """Prepare and validate the backing store for durable writes.

        Construction and reads intentionally never create filesystem state.
        OAuth application startup and mutating administrative entrypoints call
        this method explicitly before promising durable token persistence.
        """
        # Validate an existing store before creating any supporting state.  A
        # corrupt or unreadable file remains an infrastructure failure.
        self.load()
        _ensure_parent(self._path)
        _check_not_world_writable(self._path)

        parent = os.path.dirname(self._path) or "."
        probe_fd, probe_path = tempfile.mkstemp(
            dir=parent,
            prefix=".mcp_tokens_probe_",
            suffix=".tmp",
        )
        try:
            os.fchmod(probe_fd, stat.S_IRUSR | stat.S_IWUSR)
            os.fsync(probe_fd)
        finally:
            os.close(probe_fd)
            os.unlink(probe_path)

        # Mutations serialize through this exact companion lock.  Opening and
        # locking it here proves that startup can establish the same primitive
        # issuance will require later.
        with open(self._lock_path, "a+") as lock_file:
            os.chmod(self._lock_path, stat.S_IRUSR | stat.S_IWUSR)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        # Startup doubles as a retention sweep: drop expired/revoked OAuth
        # grant history now that durable state is established, before any
        # new issuance can re-grow the store. Non-OAuth records (static,
        # profile-scoped, admin) including their revoked audit trail are
        # untouched. Uses one captured ``now`` for the whole sweep.
        self.compact_expired_oauth(time.time())

    def load(self) -> list[StoredTokenEntry]:
        """Load all token entries from the store file."""
        try:
            with open(self._path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as exc:
            raise ValueError("Token store contains invalid JSON") from exc
        entries = data.get("tokens", [])
        return [_dict_to_entry(e) for e in entries]

    def add(self, entry: StoredTokenEntry) -> None:
        """Append an entry and persist atomically (locked read-modify-write)."""
        return self.add_many([entry])

    def add_many(
        self,
        entries: list[StoredTokenEntry],
        compact_oauth_now: float | None = None,
    ) -> None:
        """Append several entries in one atomic locked read-modify-write.

        When *compact_oauth_now* is supplied, stale (expired/revoked)
        OAuth records currently in the store are dropped in the same
        transaction before the new entries are appended, bounding a
        long-lived OAuth store without a separate sweep. Generic callers
        (CLI, static/operator/admin tokens) omit the argument and keep
        the previous append-only behaviour.
        """
        with self._locked():
            current = self.load()
            if compact_oauth_now is not None:
                current = self._filter_stale_oauth(current, compact_oauth_now)
            current.extend(entries)
            self._write(current)

    def rotate(
        self,
        revoke_hash: str,
        new_entries: list[StoredTokenEntry],
        compact_oauth_now: float | None = None,
    ) -> StoredTokenEntry | None:
        """Atomically revoke one entry (by hash) and append replacement entries.

        Single locked read-modify-write so a rotation is never observed
        half-applied: the old refresh is marked revoked and the new
        access/refresh pair is persisted in the same write. When
        *compact_oauth_now* is supplied, stale OAuth records — including
        the just-revoked old refresh — are dropped in the same
        transaction so revoked refresh history cannot accumulate during
        a long-lived process; every unexpired OAuth access token and all
        non-OAuth records are preserved. Returns the revoked entry, or
        ``None`` when no non-revoked entry matched.
        """
        with self._locked():
            entries = self.load()
            revoked: StoredTokenEntry | None = None
            for e in entries:
                if e.token_hash == revoke_hash and e.revoked_at is None:
                    e.revoked_at = _iso_now()
                    revoked = e
                    break
            if compact_oauth_now is not None:
                entries = self._filter_stale_oauth(entries, compact_oauth_now)
            entries.extend(new_entries)
            self._write(entries)
            return revoked

    def revoke(self, token_id: str) -> StoredTokenEntry | None:
        """Mark a token as revoked by id. Returns the entry or None.

        The read-modify-write cycle runs under the same exclusive lock
        as ``add``, so a concurrently added token cannot be overwritten
        by a stale copy that would resurrect the revoked entry.
        """
        with self._locked():
            entries = self.load()
            for e in entries:
                if e.id == token_id and e.revoked_at is None:
                    e.revoked_at = _iso_now()
                    self._write(entries)
                    return e
            return None

    def find_by_hash(self, token_hash: str) -> StoredTokenEntry | None:
        """Find an entry by its hash (exact match)."""
        for e in self.load():
            if e.token_hash == token_hash:
                return e
        return None

    def compact_expired_oauth(self, now: float) -> int:
        """Atomically remove expired or revoked OAuth grant records.

        Retention compaction is scoped strictly to ``profile == "oauth"``
        records: any such record that is revoked (``revoked_at`` set) or
        whose persisted ``expires_at`` has already passed is removed. All
        non-OAuth records (static, profile-scoped and admin credentials)
        are preserved unchanged — including their revoked audit history —
        so operator-managed state is never touched by an automatic sweep.

        The read-modify-write cycle runs under the same exclusive flock as
        ``add``/``revoke`` so a concurrent mutation cannot be lost between
        the load and the compacted write. Returns the number of records
        removed; no file write occurs when nothing is removable. Callers
        supply one captured ``now`` so a single sweep uses one clock stamp.
        """
        with self._locked():
            entries = self.load()
            kept = self._filter_stale_oauth(entries, now)
            removed = len(entries) - len(kept)
            if removed:
                self._write(kept)
            return removed

    def _filter_stale_oauth(
        self, entries: list[StoredTokenEntry], now: float
    ) -> list[StoredTokenEntry]:
        """Pure filter dropping stale/revoked OAuth records; keeps everything else.

        Applies the compaction predicate to an already-loaded list using
        one supplied ``now``; non-OAuth records (static, profile-scoped
        and admin credentials) are always preserved, including their
        revoked audit history. Caller must already hold the store lock.
        """
        return [
            entry
            for entry in entries
            if not (entry.profile == "oauth" and self._oauth_record_is_stale(entry, now))
        ]

    def _oauth_record_is_stale(self, entry: StoredTokenEntry, now: float) -> bool:
        """True when an OAuth record should be removed by compaction."""
        if entry.revoked_at is not None:
            return True
        expires = _parse_expiry_epoch(entry.expires_at)
        if expires is None:
            return False
        return now > expires

    def _locked(self) -> _LockedStore:
        """Acquire an exclusive lock over the whole read-modify-write cycle.

        Used by mutating operations so the load + write transaction is
        atomic across processes (flock on the companion .lock file).
        """
        return _LockedStore(self)

    def _write(self, entries: list[StoredTokenEntry]) -> None:
        """Atomically write entries. Caller must hold the store lock."""
        payload: dict[str, Any] = {
            "version": TOKEN_STORE_VERSION,
            "tokens": [_entry_to_dict(e) for e in entries],
        }
        raw = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

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

    def _save(self, entries: list[StoredTokenEntry]) -> None:
        """Atomically write entries to the store file.

        Backwards-compatible wrapper: acquires the exclusive lock itself,
        then delegates to ``_write``.
        """
        with self._locked():
            self._write(entries)


class _LockedStore:
    """Context manager holding an exclusive flock on the store lock file.

    Ensures mutating operations (add/revoke) are atomic across processes.
    """

    def __init__(self, store: TokenStore) -> None:
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
