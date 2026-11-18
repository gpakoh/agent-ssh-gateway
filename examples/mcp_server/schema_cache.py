"""Session-bound, fail-closed conditional schema ETags for MCP discovery.

This is a server-side opt-in contract. The external MCP client owns its cached
schema. No cache token can skip server authorization or be reused across a
different authenticated lifecycle, build, registry snapshot or Gateway state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import weakref
from threading import Lock
from typing import Any

_ETAG_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROCESS_KEY = secrets.token_bytes(32)
# Weak lifecycle keys keep historical session data bounded to active sessions.
# A changed auth/build/toolset/plane observation rotates the nonce even if the
# original value later returns (e.g. ready -> starting -> ready).
_SESSION_STATES: weakref.WeakKeyDictionary[
    object, tuple[bytes, tuple[str | None, ...]]
] = weakref.WeakKeyDictionary()
_SESSION_STATE_LOCK = Lock()


def validate_conditional_etag(value: str | None) -> str | None:
    """Reject arbitrary ETags instead of reflecting them into responses."""
    if value is None:
        return None
    if type(value) is not str or len(value) != 71 or _ETAG_RE.fullmatch(value) is None:
        raise ValueError("if_schema_etag must be sha256:<64 lowercase hex>")
    return value


def schema_etag(
    *,
    lifecycle_owner: object | None,
    auth_identity: str | None,
    build_sha: str | None,
    plane_state: str | None,
    toolset_hash: str | None,
    request: dict[str, Any],
    catalog: dict[str, Any],
) -> str | None:
    """Produce a token only for a proven, ready authenticated MCP lifecycle.

    The token contains no raw identity or service URL and is intentionally
    different for different lifecycle objects even if scopes/schemas match.
    Weak keys prevent old lifecycles from becoming an unbounded global cache.
    """
    if lifecycle_owner is None:
        return None
    # Observe invalid states as well: a later return to an identical "ready"
    # value must not reinstate the lease that predates the transition.
    observed = (
        auth_identity if isinstance(auth_identity, str) else None,
        build_sha if isinstance(build_sha, str) else None,
        plane_state if isinstance(plane_state, str) else None,
        toolset_hash if isinstance(toolset_hash, str) else None,
    )
    try:
        with _SESSION_STATE_LOCK:
            prior = _SESSION_STATES.get(lifecycle_owner)
            if prior is None or prior[1] != observed:
                nonce = secrets.token_bytes(32)
                _SESSION_STATES[lifecycle_owner] = (nonce, observed)
            else:
                nonce = prior[0]
    except TypeError:
        # Non-weakrefable/unhashable lifecycle cannot receive a safe lease.
        return None
    if (
        not isinstance(auth_identity, str)
        or not auth_identity
        or not isinstance(build_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", build_sha) is None
        or plane_state != "ready"
        or not isinstance(toolset_hash, str)
        or _ETAG_RE.fullmatch(toolset_hash) is None
    ):
        return None

    payload = json.dumps(
        {
            "v": 1,
            "auth": auth_identity,
            "session": nonce.hex(),
            "build": build_sha,
            "plane": plane_state,
            "toolset": toolset_hash,
            "request": request,
            "catalog": catalog,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hmac.new(_PROCESS_KEY, payload, hashlib.sha256).hexdigest()
