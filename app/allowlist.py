"""Four-layer hierarchical allowlist with TTL/expiration.

Layers (highest priority first): Agent > Project > User > System.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass

LAYERS = ("agent", "project", "user", "system")

SELECTOR_TYPES = ("rule_id", "exact", "prefix", "regex")

ALLOWLIST_TTL_S = 3600


def _matches_prefix(command: str, prefix: str) -> bool:
    """Prefix match with a word-boundary check after the prefix.

    A bare command.startswith(prefix) lets an entry meant to cover one
    command family also cover an unrelated one that merely happens to
    share the same leading characters — a "docker" prefix entry would
    also match "dockerize-evil.sh", not just "docker ps"/"docker-compose"
    style invocations of the actual docker family. The character right
    after the prefix must be whitespace or end-of-string; anything else
    (including a bare word-continuation letter/digit) means it's a
    different command that only looks similar as a string.
    """
    if not command.startswith(prefix):
        return False
    if len(command) == len(prefix):
        return True
    return command[len(prefix)] in (" ", "\t")


@dataclass(frozen=True)
class AllowlistEntry:
    id: str
    layer: str
    selector_type: str
    selector_value: str
    created_at: float
    expires_at: float | None = None
    created_by: str | None = None
    reason: str = ""
    ttl: int | None = None

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or time.time()) >= self.expires_at


@dataclass(frozen=True)
class AllowlistMatch:
    entry: AllowlistEntry
    command: str
    matched_at: float


class Allowlist:
    """Four-layer hierarchical allowlist with TTL."""

    def __init__(self) -> None:
        self._entries: dict[str, list[AllowlistEntry]] = {
            "agent": [],
            "project": [],
            "user": [],
            "system": [],
        }

    def add(
        self,
        layer: str,
        selector_type: str,
        selector_value: str,
        *,
        created_by: str | None = None,
        reason: str = "",
        ttl: int | None = None,
    ) -> AllowlistEntry:
        self._expire_stale()
        entry_id = uuid.uuid4().hex[:16]
        now = time.time()
        expires_at: float | None = None
        if ttl is not None:
            expires_at = now + ttl
        elif layer != "system":
            expires_at = now + ALLOWLIST_TTL_S
        entry = AllowlistEntry(
            id=entry_id,
            layer=layer,
            selector_type=selector_type,
            selector_value=selector_value,
            created_at=now,
            expires_at=expires_at,
            created_by=created_by,
            reason=reason,
            ttl=ttl,
        )
        self._entries.setdefault(layer, []).append(entry)
        return entry

    def remove(self, entry_id: str) -> bool:
        for lst in self._entries.values():
            for i, e in enumerate(lst):
                if e.id == entry_id:
                    lst.pop(i)
                    return True
        return False

    def check(
        self,
        command: str,
        *,
        agent: str | None = None,
        project: str | None = None,
        user: str | None = None,
    ) -> AllowlistMatch | None:
        self._expire_stale()
        for layer in LAYERS:
            entries = self._entries.get(layer, [])
            if not entries:
                continue
            for entry in entries:
                if entry.is_expired():
                    continue
                if self._matches(entry, command):
                    return AllowlistMatch(
                        entry=entry,
                        command=command,
                        matched_at=time.time(),
                    )
        return None

    def _matches(self, entry: AllowlistEntry, command: str) -> bool:
        if entry.selector_type == "exact":
            return command == entry.selector_value
        if entry.selector_type == "prefix":
            return _matches_prefix(command, entry.selector_value)
        if entry.selector_type == "regex":
            return bool(re.search(entry.selector_value, command))
        if entry.selector_type == "rule_id":
            from app.command_policy import scan_command
            report = scan_command(command)
            return any(f.pattern_name == entry.selector_value for f in report.findings)
        return False

    def list_layer(self, layer: str) -> list[AllowlistEntry]:
        self._expire_stale()
        return list(self._entries.get(layer, []))

    def list_all(self) -> list[AllowlistEntry]:
        self._expire_stale()
        result: list[AllowlistEntry] = []
        for layer in LAYERS:
            result.extend(self._entries.get(layer, []))
        return result

    def clear_layer(self, layer: str) -> int:
        self._expire_stale()
        lst = self._entries.get(layer, [])
        count = len(lst)
        lst.clear()
        return count

    def clear_all(self) -> int:
        count = sum(len(lst) for lst in self._entries.values())
        for lst in self._entries.values():
            lst.clear()
        return count

    def _expire_stale(self) -> int:
        now = time.time()
        count = 0
        for lst in self._entries.values():
            before = len(lst)
            lst[:] = [e for e in lst if not e.is_expired(now)]
            count += before - len(lst)
        return count


# ── Module-level singleton ─────────────────────────────────────────────────────

_ALLOWLIST: Allowlist | None = None


def get_allowlist() -> Allowlist:
    global _ALLOWLIST
    if _ALLOWLIST is None:
        _ALLOWLIST = Allowlist()
    return _ALLOWLIST


def reset_allowlist() -> None:
    """Reset singleton (for testing)."""
    global _ALLOWLIST
    _ALLOWLIST = None
