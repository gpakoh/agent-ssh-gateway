"""Access control gate — Phase 12B.

In-memory store with optional Redis backing for crash recovery.
Key: SHA-256(actor_fingerprint:source_ip)[:16] — never raw IP/fingerprint in keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class AccessDecision:
    key_hash: str
    decision: str  # "pending" | "allowed" | "denied"
    actor_fingerprint: str
    source_ip: str
    reason: str
    decided_by: str  # "operator" | "system" | "auto"
    created_at: float
    expires_at: float


@dataclass
class AccessPolicyResult:
    state: str  # "pending" | "allowed" | "denied" | "exempt"
    effective_profile: str
    reason: str
    key_hash: str


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AccessDeniedError(Exception):
    """Raised when an actor+IP tuple is explicitly denied."""


class AccessPendingApprovalError(Exception):
    """Raised when an operation requires operator approval first."""


# ---------------------------------------------------------------------------
# Key Hashing
# ---------------------------------------------------------------------------


def make_access_key_hash(actor_fingerprint: str, source_ip: str) -> str:
    """SHA-256(actor_fingerprint:source_ip), first 16 hex chars.

    Never stores raw IP or fingerprint in the key.
    """
    raw = f"{actor_fingerprint}:{source_ip}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Profile Capping
# ---------------------------------------------------------------------------


def capped_profile(requested: str) -> str:
    """Downgrade profile to readonly/testlint for pending actors."""
    if requested in ("readonly", "testlint"):
        return requested
    return "readonly"


# ---------------------------------------------------------------------------
# Access Control Store
# ---------------------------------------------------------------------------


class AccessControlStore:
    """In-memory dict with TTL, optional Redis backing.

    - Read path: memory first, return None on miss (treated as pending).
    - Write path: memory + Redis best-effort (log warning on failure).
    - Startup: load all non-expired entries from Redis into memory.
    - If Redis unavailable: continue with in-memory state.
    """

    def __init__(
        self,
        *,
        pending_ttl: int = 900,
        allow_ttl: int = 86400,
        deny_ttl: int = 86400,
        redis_url: str | None = None,
    ) -> None:
        self._store: dict[str, AccessDecision] = {}
        self._pending_ttl = pending_ttl
        self._allow_ttl = allow_ttl
        self._deny_ttl = deny_ttl
        self._redis_url = redis_url
        self._redis = None
        self._cleanup_task: asyncio.Task | None = None

    # -- TTL helper --

    def _ttl_for(self, decision: str) -> int:
        if decision == "pending":
            return self._pending_ttl
        if decision == "denied":
            return self._deny_ttl
        return self._allow_ttl

    # -- CRUD --

    def get(self, actor_fingerprint: str, source_ip: str) -> AccessDecision | None:
        key_hash = make_access_key_hash(actor_fingerprint, source_ip)
        entry = self._store.get(key_hash)
        if entry is None:
            return None
        if time.time() > entry.expires_at:
            del self._store[key_hash]
            return None
        return entry

    def set(
        self,
        actor_fingerprint: str,
        source_ip: str,
        decision: str,
        reason: str,
        decided_by: str,
        ttl_seconds: int | None = None,
    ) -> AccessDecision:
        key_hash = make_access_key_hash(actor_fingerprint, source_ip)
        now = time.time()
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl_for(decision)
        entry = AccessDecision(
            key_hash=key_hash,
            decision=decision,
            actor_fingerprint=actor_fingerprint,
            source_ip=source_ip,
            reason=reason,
            decided_by=decided_by,
            created_at=now,
            expires_at=now + ttl,
        )
        self._store[key_hash] = entry
        # Best-effort Redis write — fire and forget
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._save_to_redis(entry))
        except RuntimeError:
            pass
        return entry

    def delete(self, actor_fingerprint: str, source_ip: str) -> None:
        key_hash = make_access_key_hash(actor_fingerprint, source_ip)
        self._store.pop(key_hash, None)

    # -- Cleanup --

    def cleanup_expired(self) -> int:
        now = time.time()
        expired = [k for k, v in self._store.items() if now > v.expires_at]
        for k in expired:
            del self._store[k]
        return len(expired)

    async def start_cleanup_task(self, interval: float = 60.0) -> None:
        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval)
                count = self.cleanup_expired()
                if count:
                    logger.debug("access_control.cleanup_evicted %d entries", count)

        self._cleanup_task = asyncio.create_task(_loop())

    async def stop_cleanup_task(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    # -- Redis (best-effort) --

    async def _get_redis(self):
        if self._redis is not None:
            return self._redis
        if not self._redis_url:
            return None
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            return self._redis
        except Exception as exc:
            logger.warning("access_control.redis_connect_failed: %s", exc)
            return None

    async def load_from_redis(self) -> int:
        try:
            r = await self._get_redis()
        except Exception as exc:
            logger.warning("access_control.redis_unavailable: %s", exc)
            return 0
        if r is None:
            return 0
        loaded = 0
        try:
            cursor = 0
            while True:
                cursor, keys = await r.scan(cursor, match="ac:*", count=200)
                for key in keys:
                    try:
                        data = await r.hgetall(key)
                        if not data:
                            continue
                        expires_at = float(data.get("expires_at", "0"))
                        if time.time() > expires_at:
                            continue
                        entry = AccessDecision(
                            key_hash=data["key_hash"],
                            decision=data["decision"],
                            actor_fingerprint=data["actor_fingerprint"],
                            source_ip=data["source_ip"],
                            reason=data.get("reason", ""),
                            decided_by=data.get("decided_by", "system"),
                            created_at=float(data.get("created_at", "0")),
                            expires_at=expires_at,
                        )
                        self._store[entry.key_hash] = entry
                        loaded += 1
                    except Exception as exc:
                        logger.warning("access_control.redis_load_entry_error: %s", exc)
                if cursor == 0:
                    break
        except Exception as exc:
            logger.warning("access_control.redis_unavailable: %s", exc)
        return loaded

    async def _save_to_redis(self, entry: AccessDecision) -> None:
        if not self._redis_url:
            return
        try:
            r = await self._get_redis()
            if r is None:
                return
            key = f"ac:{entry.key_hash}"
            await r.hset(
                key,
                mapping={
                    "key_hash": entry.key_hash,
                    "decision": entry.decision,
                    "actor_fingerprint": entry.actor_fingerprint,
                    "source_ip": entry.source_ip,
                    "reason": entry.reason,
                    "decided_by": entry.decided_by,
                    "created_at": str(entry.created_at),
                    "expires_at": str(entry.expires_at),
                },
            )
            ttl = max(1, int(entry.expires_at - time.time()))
            await r.expire(key, ttl)
        except Exception as exc:
            logger.warning("access_control.redis_save_failed: %s", exc)

    # -- Policy Resolution --

    def resolve_access_policy(
        self,
        *,
        actor_fingerprint: str,
        token_type: str,
        source_ip: str,
        requested_profile: str,
        enforce_master: bool = False,
    ) -> AccessPolicyResult:
        key_hash = make_access_key_hash(actor_fingerprint, source_ip)

        # Master exempt by default (break-glass)
        if token_type == "master" and not enforce_master:
            return AccessPolicyResult(
                state="exempt",
                effective_profile=requested_profile,
                reason="master identity exempt from access gate",
                key_hash=key_hash,
            )

        entry = self.get(actor_fingerprint, source_ip)

        if entry is None or entry.decision == "pending":
            return AccessPolicyResult(
                state="pending",
                effective_profile=capped_profile(requested_profile),
                reason="unknown or pending actor — profile capped",
                key_hash=key_hash,
            )

        if entry.decision == "denied":
            raise AccessDeniedError(
                f"actor {actor_fingerprint[:12]}... denied: {entry.reason}"
            )

        # allowed
        return AccessPolicyResult(
            state="allowed",
            effective_profile=requested_profile,
            reason=entry.reason,
            key_hash=key_hash,
        )
