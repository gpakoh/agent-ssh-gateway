"""Connection pooling for idle SSH transports.

Reuses idle SSH connections per (host, port, username, auth_method,
credential_fingerprint) tuple instead of paying the TCP handshake cost on
every connect. The credential fingerprint (see
app.ssh_manager._credential_fingerprint) is a one-way hash of the presented
password/key — a caller who doesn't already know the exact credential that
authenticated the pooled transport simply misses the pool and goes through
a normal fresh authentication instead of being handed someone else's
already-authenticated connection. Optional — the pool is only created when
SSH_CONNECTION_POOL_SIZE > 0, so existing deployments are unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

import paramiko

logger = logging.getLogger(__name__)


class PooledConnection:
    """A client parked in the pool with its last-use timestamp."""

    __slots__ = ("client", "last_used")

    def __init__(self, client: paramiko.SSHClient) -> None:
        self.client = client
        self.last_used = time.monotonic()

    def touch(self) -> None:
        self.last_used = time.monotonic()


class ConnectionPool:
    """In-memory pool of idle SSH clients.

    Key: (host, port, username, auth_method, credential_fingerprint) where
    auth_method is "password" or "key" and credential_fingerprint is a
    one-way hash of the actual credential presented (see
    app.ssh_manager._credential_fingerprint) — two different credentials
    for the same host/port/username never share a pool entry. Supports LRU
    eviction (oldest idle connection is dropped when the pool exceeds its
    size) and TTL expiry.

    All operations are safe to call from the event loop; internal state is
    guarded by an asyncio.Lock.
    """

    def __init__(self, max_size: int = 0, ttl_seconds: int = 60) -> None:
        self._max_size = max(0, max_size)
        self._ttl_seconds = max(0, ttl_seconds)
        self._lock = asyncio.Lock()
        # key -> deque of PooledConnection (most recent use at the end)
        self._idle: dict[tuple, deque] = defaultdict(deque)
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._max_size > 0

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    @property
    def max_size(self) -> int:
        return self._max_size

    def idle_count(self) -> int:
        return sum(len(q) for q in self._idle.values())

    def stats(self) -> dict[str, int]:
        """Return {idle, hits, misses, evictions} snapshot."""
        return {
            "idle": self.idle_count(),
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
        }

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def acquire(self, key: tuple) -> paramiko.SSHClient | None:
        """Take an idle client for key, or None on miss.

        Expired (TTL) and dead transports are dropped and the miss path is
        taken instead. A miss is counted once per acquire call that returns
        None; a hit once per acquire that returns a usable client.
        """
        if not self.enabled:
            self._misses += 1
            return None

        async with self._lock:
            queue = self._idle.get(key)
            if not queue:
                self._misses += 1
                return None

            now = time.monotonic()
            while queue:
                conn = queue.pop()
                try:
                    transport = conn.client.get_transport()
                    alive = transport is not None and transport.is_active()
                except Exception:
                    alive = False
                if not alive or (self._ttl_seconds and now - conn.last_used > self._ttl_seconds):
                    # dead or stale — drop it, keep looking
                    self._close_client(conn.client)
                    continue
                conn.touch()
                self._hits += 1
                return conn.client

            self._misses += 1
            return None

    async def release(self, key: tuple, client: paramiko.SSHClient) -> None:
        """Park a client back in the pool (or close it if the pool is full)."""
        if not self.enabled:
            self._close_client(client)
            return

        async with self._lock:
            queue = self._idle[key]
            if self.idle_count() >= self._max_size:
                self._evict_lru_locked()
            queue.append(PooledConnection(client))

    async def evict_stale(self) -> int:
        """Drop connections idle longer than TTL. Returns count dropped."""
        if not self.enabled or self._ttl_seconds <= 0:
            return 0
        now = time.monotonic()
        dropped = 0
        async with self._lock:
            for key in list(self._idle.keys()):
                queue = self._idle[key]
                while queue:
                    oldest = queue[0]
                    if now - oldest.last_used > self._ttl_seconds:
                        queue.popleft()
                        self._close_client(oldest.client)
                        dropped += 1
                    else:
                        break
                if not queue:
                    del self._idle[key]
        if dropped:
            logger.info("Pool evicted %d stale connection(s)", dropped)
        return dropped

    async def close_all(self) -> int:
        """Close and drain every idle connection. Returns count closed."""
        async with self._lock:
            keys = list(self._idle.keys())
            clients = [q.popleft().client for k in keys for q in [self._idle[k]] for _ in range(len(self._idle[k]))]
            self._idle.clear()
        for client in clients:
            self._close_client(client)
        if clients:
            logger.info("Pool closed %d idle connection(s)", len(clients))
        return len(clients)

    # ------------------------------------------------------------------
    # Internals (callers must hold the lock where noted)
    # ------------------------------------------------------------------

    def _evict_lru_locked(self) -> None:
        """Drop the oldest idle connection across all keys (LRU eviction).

        Caller must hold self._lock.
        """
        oldest_key: tuple | None = None
        oldest_conn: PooledConnection | None = None
        for key, queue in self._idle.items():
            if not queue:
                continue
            candidate = queue[0]
            if oldest_conn is None or candidate.last_used < oldest_conn.last_used:
                oldest_key, oldest_conn = key, candidate
        if oldest_key is not None and oldest_conn is not None:
            self._idle[oldest_key].popleft()
            self._close_client(oldest_conn.client)
            self._evictions += 1

    @staticmethod
    def _close_client(client: paramiko.SSHClient) -> None:
        try:
            client.close()
        except Exception as exc:
            logger.debug("Error closing pooled client: %s", exc)
