"""Write permission modes for the experimental MCP server."""

from __future__ import annotations

import os
from typing import Literal, cast

WriteMode = Literal["off", "handoff", "full"]

DEFAULT_WRITE_MODE: WriteMode = "off"
ALLOWED_WRITE_MODES: set[str] = {"off", "handoff", "full"}


class WriteModeError(ValueError):
    """Raised when the MCP write mode is invalid."""


class WritePermissionError(PermissionError):
    """Raised when a write operation is not allowed."""


def get_write_mode() -> WriteMode:
    """Return configured MCP write mode."""
    raw = os.environ.get("MCP_GATEWAY_WRITE_MODE", DEFAULT_WRITE_MODE).strip().lower()
    if raw not in ALLOWED_WRITE_MODES:
        allowed = ", ".join(sorted(ALLOWED_WRITE_MODES))
        raise WriteModeError(
            f"Invalid MCP_GATEWAY_WRITE_MODE={raw!r}; expected one of: {allowed}"
        )
    return cast(WriteMode, raw)


def assert_handoff_write_allowed(mode: WriteMode | None = None) -> None:
    """Raise if writing .ai-bridge handoff files is not allowed."""
    selected_mode = mode or get_write_mode()
    if selected_mode not in {"handoff", "full"}:
        raise WritePermissionError(
            "Handoff writes are disabled. Set MCP_GATEWAY_WRITE_MODE=handoff "
            "to enable .ai-bridge/current-plan.md writes."
        )
