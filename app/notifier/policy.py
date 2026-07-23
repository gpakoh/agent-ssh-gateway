"""Alert policy primitives: severity mapping, dedup keys, delivery classification."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

_SEVERITY_MAP: dict[str, str] = {
    "command.deny": "warning",
    "workspace.readonly_block": "warning",
    "system.error": "critical",
    "session.connect": "info",
    "session.disconnect": "info",
    "health.degraded": "warning",
    "health.unreachable": "critical",
    "health.recovered": "info",
}


def severity_for_event(event_type: str) -> str:
    """Return severity level for an event type.

    Returns "info" for unknown types (safe default).
    """
    return _SEVERITY_MAP.get(event_type, "info")


# ---------------------------------------------------------------------------
# Dedup key builder
# ---------------------------------------------------------------------------

# Safe bounded fields used for dedup key construction.
_DEDUP_FIELDS = ("event_type", "route", "error_code", "profile", "decision")


def build_dedup_key(event: Mapping[str, Any]) -> str:
    """Build a dedup key from safe bounded fields ONLY.

    The key is deterministic for identical logical events regardless of
    request_id, event_id, timestamp, target_id, source_ip, host, path,
    or raw reason text.

    Includes metadata.command_root when present (command_root is a
    normalized, bounded field — not raw command text).

    Returns a 16-char hex SHA-256 prefix.
    """
    parts: list[str] = []
    for field in _DEDUP_FIELDS:
        val = str(event.get(field) or "")
        if val:
            parts.append(f"{field}={val}")

    metadata = event.get("metadata")
    if isinstance(metadata, Mapping):
        command_root = str(metadata.get("command_root") or "")
        if command_root:
            parts.append(f"command_root={command_root}")

    if not parts:
        return ""

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Delivery classification
# ---------------------------------------------------------------------------


def classify_event_delivery(
    event_type: str,
    realtime_types: tuple[str, ...],
    digest_types: tuple[str, ...],
) -> str:
    """Classify an event as 'realtime', 'digest', or 'skip'.

    Realtime events are sent immediately. Digest events are batched.
    Unknown event types are skipped.
    """
    if event_type in realtime_types:
        return "realtime"
    if event_type in digest_types:
        return "digest"
    return "skip"
