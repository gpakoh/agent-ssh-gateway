"""Persistent metadata-only audit trail — JSONL append + bounded ring buffer.

Design:
    - AuditEvent: frozen dataclass with metadata-only fields (no command output,
      no file content, no secrets).
    - AuditEventLogger: appends JSONL, keeps bounded recent ring buffer,
      tolerates write errors without breaking caller flow.
    - redact_secrets(): defensive redaction reused from security module.

Config:
    AUDIT_LOG_PATH   — JSONL file path (default: ./data/audit/events.jsonl)
    AUDIT_RECENT_LIMIT — max in-memory recent events (default: 500)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Secret redaction (defensive, standalone — mirrors security.redact_secrets)
# ---------------------------------------------------------------------------

SECRET_REDACTION_PLACEHOLDER = "[REDACTED]"

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+"),
        r"\1=" + SECRET_REDACTION_PLACEHOLDER,
    ),
    (
        re.compile(r"(?i)(token|secret|api[_-]?key)\s*[:=]\s*\S+"),
        r"\1=" + SECRET_REDACTION_PLACEHOLDER,
    ),
    (
        re.compile(r"(?i)(authorization:\s*Bearer\s+)\S+"),
        r"\1" + SECRET_REDACTION_PLACEHOLDER,
    ),
    (
        re.compile(r"(?i)(sshpass\s+-p\s+)\S+"),
        r"\1" + SECRET_REDACTION_PLACEHOLDER,
    ),
]


def redact_secrets(value: Any) -> Any:
    """Redact obvious secrets from strings, dicts, and lists.

    This is a safety net for audit records. It is not a full DLP system.
    """
    if value is None:
        return None

    if isinstance(value, str):
        redacted = value
        for pattern, replacement in _SECRET_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(
                r"(?i)(api[_-]?key|token|secret|password|passwd|pwd|authorization|private[_-]?key)",
                key_text,
            ):
                result[key] = SECRET_REDACTION_PLACEHOLDER
            else:
                result[key] = redact_secrets(item)
        return result

    if isinstance(value, list):
        return [redact_secrets(item) for item in value]

    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)

    return value


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class AuditEventType(StrEnum):
    """Category of auditable event."""

    COMMAND_EXECUTE = "command.execute"
    COMMAND_DENY = "command.deny"
    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    FILE_EDIT = "file.edit"
    FILE_PATCH = "file.patch"
    SESSION_CONNECT = "session.connect"
    SESSION_DISCONNECT = "session.disconnect"
    AUTH_CHECK = "auth.check"
    WORKSPACE_READONLY_BLOCK = "workspace.readonly_block"
    POLICY_EVALUATE = "policy.evaluate"
    MCP_TOOL = "mcp.tool"
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_ERROR = "system.error"


class Decision(StrEnum):
    """Policy decision outcome."""

    ALLOWED = "allowed"
    DENIED = "denied"
    ERROR = "error"


# ---------------------------------------------------------------------------
# AuditEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEvent:
    """Metadata-only audit event. No command output, no file content, no secrets."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="milliseconds")
    )
    event_type: str = ""
    actor_type: str = ""  # e.g. "api_key", "agent_token", "jwt", "system"
    actor_name: str = ""
    actor_fingerprint: str = ""  # SHA-256 fingerprint, never raw key
    request_id: str = ""
    source_ip: str = ""
    route: str = ""  # e.g. "POST /api/ssh/execute"
    tool: str = ""  # MCP tool name when applicable
    action: str = ""  # human-readable action description
    target_type: str = ""  # e.g. "session", "file", "job"
    target_id: str = ""  # session_id, file path, job_id
    policy: str = ""  # policy profile used
    profile: str = ""  # resolved profile name
    decision: str = Decision.ALLOWED
    reason: str = ""
    error_code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict, stripping empty values for compact JSONL."""
        d = asdict(self)
        # Strip empty strings, empty dicts, and empty lists
        return {k: v for k, v in d.items() if v not in ("", {}, [])}


# ---------------------------------------------------------------------------
# AuditEventLogger
# ---------------------------------------------------------------------------


class AuditEventLogger:
    """Append-only JSONL audit logger with bounded recent ring buffer.

    Tolerates write errors without breaking caller flow.
    Thread-safe for concurrent append calls.
    """

    def __init__(self, log_path: str, recent_limit: int = 500) -> None:
        self._log_path = Path(log_path)
        self._recent_limit = recent_limit
        self._recent: deque[AuditEvent] = deque(maxlen=recent_limit)
        self._lock = threading.Lock()

        # Ensure parent directory exists
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    # -- public API --------------------------------------------------------

    def append(self, event: AuditEvent) -> None:
        """Append event to JSONL file and ring buffer.

        Write errors are logged but never propagated to caller.
        """
        redacted_event = self._redact_event(event)

        with self._lock:
            self._recent.append(redacted_event)

        try:
            line = json.dumps(redacted_event.to_dict(), ensure_ascii=False, default=str)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            logger.warning("audit: failed to write event %s to %s", event.event_id, self._log_path)
        except Exception:
            logger.warning("audit: unexpected error writing event %s", event.event_id, exc_info=True)

    def recent(self, n: int | None = None) -> list[AuditEvent]:
        """Return the most recent events (newest last).

        Args:
            n: max events to return. None = all buffered events.
        """
        with self._lock:
            if n is None:
                return list(self._recent)
            return list(self._recent)[-n:]

    @property
    def recent_count(self) -> int:
        """Number of events currently in the ring buffer."""
        with self._lock:
            return len(self._recent)

    @property
    def log_path(self) -> Path:
        return self._log_path

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _redact_event(event: AuditEvent) -> AuditEvent:
        """Defensively redact secrets from event fields.

        Returns a new AuditEvent with redacted values. Frozen dataclass
        means we reconstruct rather than mutate.
        """
        return AuditEvent(
            event_id=event.event_id,
            timestamp=event.timestamp,
            event_type=event.event_type,
            actor_type=event.actor_type,
            actor_name=redact_secrets(event.actor_name),
            actor_fingerprint=event.actor_fingerprint,
            request_id=event.request_id,
            source_ip=event.source_ip,
            route=event.route,
            tool=event.tool,
            action=redact_secrets(event.action),
            target_type=event.target_type,
            target_id=redact_secrets(event.target_id),
            policy=event.policy,
            profile=event.profile,
            decision=event.decision,
            reason=redact_secrets(event.reason),
            error_code=event.error_code,
            metadata=redact_secrets(event.metadata) if event.metadata else {},
        )


# ---------------------------------------------------------------------------
# Convenience helpers for common audit patterns
# ---------------------------------------------------------------------------


def emit_command_policy_decision(
    *,
    event_logger: AuditEventLogger | None,
    command: str,
    session_id: str,
    effective_profile: str,
    decision_allowed: bool,
    decision_reason: str,
    command_root: str | None = None,
    source_ip: str = "",
    route: str = "",
    actor_fingerprint: str = "",
    request_id: str = "",
) -> None:
    """Emit a COMMAND_DENY or COMMAND_EXECUTE event for command policy decisions."""
    if not event_logger:
        return
    event_logger.append(AuditEvent(
        event_type=(
            AuditEventType.COMMAND_EXECUTE if decision_allowed
            else AuditEventType.COMMAND_DENY
        ),
        actor_type="api_key",
        actor_fingerprint=actor_fingerprint,
        request_id=request_id,
        source_ip=source_ip,
        route=route,
        action=f"command {'allowed' if decision_allowed else 'denied'} by policy",
        target_type="session",
        target_id=session_id,
        profile=effective_profile,
        decision=Decision.ALLOWED if decision_allowed else Decision.DENIED,
        reason=decision_reason,
        metadata={"command_root": command_root} if command_root else {},
    ))
