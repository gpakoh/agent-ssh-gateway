"""MCP-local structured audit logger — metadata-only JSONL.

Designed for the MCP process (separate from gateway). Captures security-relevant
decisions without command output, secrets, or full prompt/task content.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config (read from env at import time)
# ---------------------------------------------------------------------------

MCP_AUDIT_LOG_PATH = os.environ.get("MCP_AUDIT_LOG_PATH", "logs/mcp_audit.jsonl")
MCP_AUDIT_RECENT_LIMIT = int(os.environ.get("MCP_AUDIT_RECENT_LIMIT", "500"))


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

SECRET_PLACEHOLDER = "[REDACTED]"

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+"), r"\1=" + SECRET_PLACEHOLDER),
    (re.compile(r"(?i)(token|secret|api[_-]?key)\s*[:=]\s*\S+"), r"\1=" + SECRET_PLACEHOLDER),
    (re.compile(r"(?i)(authorization:\s*Bearer\s+)\S+"), r"\1" + SECRET_PLACEHOLDER),
    (re.compile(r"(?i)(sshpass\s+-p\s+)\S+"), r"\1" + SECRET_PLACEHOLDER),
]


def redact_secrets(value: Any) -> Any:
    """Defensively redact obvious secrets from strings, dicts, lists."""
    if value is None:
        return None
    if isinstance(value, str):
        result = value
        for pat, repl in _SECRET_PATTERNS:
            result = pat.sub(repl, result)
        return result
    if isinstance(value, dict):
        return {
            k: SECRET_PLACEHOLDER if re.search(r"(?i)(api[_-]?key|token|secret|password|passwd|pwd|authorization|private[_-]?key)", str(k)) else redact_secrets(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(i) for i in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(i) for i in value)
    return value


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------

# Fields that must NEVER appear in audit
FORBIDDEN_KEYS = frozenset({
    "output", "stdout", "stderr", "content", "patch",
    "old_string", "new_string", "prompt", "task",
})


@dataclass(frozen=True)
class McpAuditEvent:
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    event_type: str = ""           # e.g. "mcp.tool_blocked", "mcp.command_denied"
    actor_type: str = "mcp_client"
    tool: str = ""                 # tool name
    action: str = ""               # what was attempted
    decision: str = ""             # "deny", "block", "error"
    reason: str = ""               # human-readable
    error_code: str = ""           # structured code
    request_id: str = ""           # optional correlation
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class McpAuditLogger:
    """JSONL audit logger with bounded in-memory ring buffer."""

    def __init__(
        self,
        log_path: str = MCP_AUDIT_LOG_PATH,
        recent_limit: int = MCP_AUDIT_RECENT_LIMIT,
    ) -> None:
        self._log_path = log_path
        self._recent_limit = recent_limit
        self._buffer: list[dict[str, Any]] = []

    def append(self, event: McpAuditEvent) -> None:
        """Append event to JSONL file and in-memory buffer."""
        record = redact_secrets(asdict(event))
        # Strip forbidden keys from metadata if present
        if "metadata" in record and isinstance(record["metadata"], dict):
            record["metadata"] = {
                k: v for k, v in record["metadata"].items() if k not in FORBIDDEN_KEYS
            }
        # Ring buffer
        self._buffer.append(record)
        if len(self._buffer) > self._recent_limit:
            self._buffer = self._buffer[-self._recent_limit:]
        # JSONL append (non-fatal)
        try:
            Path(self._log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass  # non-fatal

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return recent events from ring buffer."""
        n = limit or self._recent_limit
        return list(self._buffer[-n:])


# ---------------------------------------------------------------------------
# Module-level singleton (created once, importable by other modules)
# ---------------------------------------------------------------------------

_audit_logger: McpAuditLogger | None = None


def get_audit_logger() -> McpAuditLogger:
    """Return the module-level singleton McpAuditLogger."""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = McpAuditLogger()
    return _audit_logger
