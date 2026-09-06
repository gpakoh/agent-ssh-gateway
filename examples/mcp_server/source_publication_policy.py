"""Pure source-selection and sanitized failure-classification policy."""

from __future__ import annotations

import re
from enum import StrEnum


class LocalSourceState(StrEnum):
    """Observed completeness of the registered local Git source."""

    FULL = "full"
    SHALLOW = "shallow"
    MISSING_COMMIT = "missing_commit"


class PublicationRoute(StrEnum):
    """Mechanism allowed to materialize the requested exact commit."""

    LOCAL = "local"
    TRUSTED_REMOTE = "trusted_remote"


class SourceFailureCause(StrEnum):
    """Stable sanitized causes safe to expose to MCP clients."""

    SHALLOW = "shallow"
    MISSING_REF = "missing_ref"
    AUTH = "auth"
    TRUST = "trust"
    NETWORK = "network"
    TIMEOUT = "timeout"
    PERMISSION = "permission"
    CORRUPTION = "corruption"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "source_materialization_failed"


_AUTH_PATTERNS = (
    re.compile(r"\bauth(?:entication|orization)? failed\b"),
    re.compile(r"\bcould not read username\b"),
    re.compile(r"\brequires gitea_token\b"),
    re.compile(r"\brequires configured credentials\b"),
    re.compile(r"\baccess denied\b"),
    re.compile(r"\breturned error:\s*(?:401|403)\b"),
    re.compile(r"\bhttp(?:s)?\s+(?:status\s+)?(?:401|403)\b"),
)

_TRUST_PATTERNS = (
    re.compile(r"\bhost key verification failed\b"),
    re.compile(r"\bserver certificate verification failed\b"),
    re.compile(r"\bssl certificate problem\b"),
    re.compile(r"\btls certificate\b"),
    re.compile(r"\bknown_hosts\b"),
)

_NETWORK_PATTERNS = (
    re.compile(r"\bcould not resolve host\b"),
    re.compile(r"\bfailed to connect\b"),
    re.compile(r"\bconnection refused\b"),
    re.compile(r"\bnetwork is unreachable\b"),
    re.compile(r"\bno route to host\b"),
)

_MISSING_REF_PATTERNS = (
    re.compile(r"\bnot our ref\b"),
    re.compile(r"\bcould(?:n't| not) find remote ref\b"),
    re.compile(r"\bdid not contain the requested commit\b"),
    re.compile(r"\bfatal:\s*(?:bad object|not a valid object name)\b"),
)

_CORRUPTION_PATTERNS = (
    re.compile(r"\bcorrupt(?:ed|ion)?\b"),
    re.compile(r"\binvalid bundle\b"),
    re.compile(r"\bbad pack\b"),
    re.compile(r"\bindex-pack failed\b"),
    re.compile(r"\bfsck\b.*\bfailed\b"),
)


def choose_publication_route(local_state: LocalSourceState) -> PublicationRoute:
    """Choose local objects only when they are known complete for publication."""
    if local_state is LocalSourceState.FULL:
        return PublicationRoute.LOCAL
    if local_state in {LocalSourceState.SHALLOW, LocalSourceState.MISSING_COMMIT}:
        return PublicationRoute.TRUSTED_REMOTE
    raise ValueError(f"unsupported local source state: {local_state!r}")


def classify_source_failure_message(message: str) -> SourceFailureCause:
    """Map implementation diagnostics to one bounded, non-secret cause."""
    detail = message.lower()
    if "shallow" in detail:
        return SourceFailureCause.SHALLOW
    if "timed out" in detail or "timeout" in detail:
        return SourceFailureCause.TIMEOUT
    if any(pattern.search(detail) for pattern in _TRUST_PATTERNS):
        return SourceFailureCause.TRUST
    if any(pattern.search(detail) for pattern in _AUTH_PATTERNS):
        return SourceFailureCause.AUTH
    if any(pattern.search(detail) for pattern in _NETWORK_PATTERNS):
        return SourceFailureCause.NETWORK
    if any(pattern.search(detail) for pattern in _MISSING_REF_PATTERNS):
        return SourceFailureCause.MISSING_REF
    if "permission denied" in detail or "not permitted" in detail:
        return SourceFailureCause.PERMISSION
    if any(pattern.search(detail) for pattern in _CORRUPTION_PATTERNS):
        return SourceFailureCause.CORRUPTION
    if "unavailable" in detail or "could not start" in detail:
        return SourceFailureCause.UNAVAILABLE
    return SourceFailureCause.UNKNOWN
