"""Compatibility façade for Py Coordination's shared, transport-free core.

MCP and the web Gateway both depend on app.coordination_logic. Keep legacy
imports stable without allowing app/* to depend on the MCP composition layer.
"""

from app.coordination_logic import (
    CoordinationOutcomeUnknown,
    _header,
    _issue,
    _pages,
    _parse,
    _same_event_payload,
    _same_issue_payload,
    _text,
    board_from_threads,
    create_thread,
    list_threads,
    list_threads_preview,
    open_thread,
    validate_key,
    validate_project,
    write_event,
)

__all__ = (
    "CoordinationOutcomeUnknown",
    "_header",
    "_issue",
    "_pages",
    "_parse",
    "_same_event_payload",
    "_same_issue_payload",
    "_text",
    "board_from_threads",
    "create_thread",
    "list_threads",
    "list_threads_preview",
    "open_thread",
    "validate_key",
    "validate_project",
    "write_event",
)

