"""Session-scoped diagnostics for external MCP tool-surface parity.

The server-local MCP tool manager remains authoritative for registration.  Any
external tool list supplied by a client is an unverified attestation used only
for diagnostics; it must never become process-global truth or an implicit
mutation precondition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_CLIENT_VISIBLE_TOOL_NAMES = 256
MAX_CLIENT_VISIBLE_TOOL_NAME_BYTES = 128
MAX_CLIENT_VISIBLE_TOOL_NAMES_BYTES = 16 * 1024
MAX_REQUIRED_GUARD_TOOL_NAMES = 32
MAX_CLIENT_TOOLSET_HASH_BYTES = 128

_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_TOOLSET_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class ClientSurfaceAttestation:
    """One client-reported tool catalog bound to one server lifecycle."""

    auth_identity: str
    toolset_hash: str
    names: tuple[str, ...]
    complete: bool


_SESSION_ATTESTATIONS: dict[object, ClientSurfaceAttestation] = {}


def _normalize_tool_names(names: list[str], *, max_names: int) -> tuple[str, ...]:
    if not isinstance(names, list):
        raise ValueError("tool names must be a list")
    if len(names) > max_names:
        raise ValueError(f"tool names exceed maximum count {max_names}")

    total_bytes = 0
    normalized: list[str] = []
    for name in names:
        if not isinstance(name, str):
            raise ValueError("tool name must be a string")
        encoded = name.encode("utf-8")
        if not encoded:
            raise ValueError("tool name must not be empty")
        if len(encoded) > MAX_CLIENT_VISIBLE_TOOL_NAME_BYTES:
            raise ValueError(
                f"tool name exceeds {MAX_CLIENT_VISIBLE_TOOL_NAME_BYTES} UTF-8 bytes"
            )
        total_bytes += len(encoded)
        if total_bytes > MAX_CLIENT_VISIBLE_TOOL_NAMES_BYTES:
            raise ValueError(
                f"tool names exceed {MAX_CLIENT_VISIBLE_TOOL_NAMES_BYTES} aggregate UTF-8 bytes"
            )
        if _TOOL_NAME_RE.fullmatch(name) is None:
            raise ValueError(f"invalid tool name: {name!r}")
        normalized.append(name)

    return tuple(sorted(set(normalized)))


def normalize_client_visible_tool_names(names: list[str]) -> tuple[str, ...]:
    """Validate, deduplicate, and sort one client-visible tool report."""

    return _normalize_tool_names(names, max_names=MAX_CLIENT_VISIBLE_TOOL_NAMES)


def normalize_required_guard_tool_names(names: list[str]) -> tuple[str, ...]:
    """Validate bounded call-context guard requirements."""

    return _normalize_tool_names(names, max_names=MAX_REQUIRED_GUARD_TOOL_NAMES)


def normalize_client_toolset_hash(value: str | None) -> str | None:
    """Bound and validate a client-reported toolset hash.

    ``None`` means the client did not know (or did not supply) the toolset hash
    its external catalog was built from, which is rendered as ``unproven`` -- an
    honest "cannot tell" rather than a false ``current`` or ``stale``.  Anything
    else must be a ``sha256:<64 lowercase hex>`` string; malformed or oversized
    input raises before any state write, mirroring the tool-name rules.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("client catalog toolset hash must be a string")
    if len(value.encode("utf-8")) > MAX_CLIENT_TOOLSET_HASH_BYTES:
        raise ValueError("client catalog toolset hash exceeds size limit")
    if _TOOLSET_HASH_RE.fullmatch(value) is None:
        raise ValueError(
            "client catalog toolset hash must match sha256:<64 lowercase hex>"
        )
    return value


def build_catalog_refresh_contract(
    *,
    server_toolset_hash: str | None,
    client_catalog_toolset_hash: str | None,
) -> dict[str, Any]:
    """Type a client catalog against the live server surface.

    Returns a ``current`` / ``stale`` / ``unproven`` state and, when stale, a
    safe refresh action.  This is diagnostic only: it never gates a server-side
    mutation, never filters ``tools/list``, and never re-registers anything.
    A long-lived client whose catalog was built from an older build/toolset hash
    can therefore detect staleness explicitly instead of inferring it from an
    empty diff, and refresh by re-running ``tools/list`` (the server registers
    its full surface at startup, so no server-side reconnect is required).
    """

    if server_toolset_hash is None or client_catalog_toolset_hash is None:
        state = "unproven"
        stale = False
    elif client_catalog_toolset_hash == server_toolset_hash:
        state = "current"
        stale = False
    else:
        state = "stale"
        stale = True

    return {
        "catalog_state": state,
        "server_toolset_hash": server_toolset_hash,
        "client_catalog_toolset_hash": client_catalog_toolset_hash,
        "refresh_required": stale,
        "refresh_action": {
            "method": "mcp",
            "tool": "tools/list",
            "transport": "streamable-http",
            "server_side_reconnect_required": False,
        },
        "note": (
            "A stale catalog means the client-supplied toolset hash differs from "
            "the current server toolset hash. Re-run mcp tools/list to obtain the "
            "current callable surface; the server registers its full surface at "
            "startup, so no server-side reconnect is required. Diagnostic only: "
            "this never gates a server mutation."
        ),
    }


def record_session_attestation(
    lifecycle_owner: object,
    auth_identity: str,
    toolset_hash: str,
    names: list[str],
    complete: bool,
) -> ClientSurfaceAttestation:
    """Store a validated attestation under the server-created lifecycle object."""

    if lifecycle_owner is None:
        raise ValueError("lifecycle_owner is required")
    if not isinstance(auth_identity, str) or not auth_identity:
        raise ValueError("auth_identity is required")
    if not isinstance(toolset_hash, str) or not toolset_hash:
        raise ValueError("toolset_hash is required")
    normalized = normalize_client_visible_tool_names(names)
    attestation = ClientSurfaceAttestation(
        auth_identity=auth_identity,
        toolset_hash=toolset_hash,
        names=normalized,
        complete=bool(complete),
    )
    _SESSION_ATTESTATIONS[lifecycle_owner] = attestation
    return attestation


def load_session_attestation(
    lifecycle_owner: object | None,
    auth_identity: str | None,
    toolset_hash: str | None,
) -> ClientSurfaceAttestation | None:
    """Load only an attestation still bound to the current identity/toolset."""

    if lifecycle_owner is None or auth_identity is None or toolset_hash is None:
        return None
    attestation = _SESSION_ATTESTATIONS.get(lifecycle_owner)
    if attestation is None:
        return None
    if attestation.auth_identity != auth_identity or attestation.toolset_hash != toolset_hash:
        return None
    return attestation


def clear_session_attestation(lifecycle_owner: object) -> None:
    """Forget exactly one lifecycle's client attestation."""

    _SESSION_ATTESTATIONS.pop(lifecycle_owner, None)


def clear_all_attestations_for_tests() -> None:
    """Reset process-local test state."""

    _SESSION_ATTESTATIONS.clear()


def evaluate_required_guards(
    attestation: ClientSurfaceAttestation | None,
    required_guard_tools: tuple[str, ...],
) -> dict[str, Any]:
    """Evaluate only explicitly declared call-context guard requirements.

    Results deliberately use "reported" wording because the external catalog
    is client-supplied and never independently verified by this server.
    """

    required = normalize_required_guard_tool_names(list(required_guard_tools))
    if not required:
        return {
            "status": "no_dependency",
            "required": [],
            "reported_present": [],
            "reported_absent": [],
            "unknown": [],
        }

    if attestation is None:
        return {
            "status": "unknown",
            "required": list(required),
            "reported_present": [],
            "reported_absent": [],
            "unknown": list(required),
        }

    reported = set(attestation.names)
    present = [name for name in required if name in reported]
    omitted = [name for name in required if name not in reported]
    if attestation.complete:
        absent = omitted
        unknown: list[str] = []
        status = "reported_absent" if absent else "reported_present"
    else:
        absent = []
        unknown = omitted
        status = "unknown" if unknown else "reported_present"

    return {
        "status": status,
        "required": list(required),
        "reported_present": present,
        "reported_absent": absent,
        "unknown": unknown,
    }
