"""User-safe Telegram message formatting for gateway audit events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_ALERT_TITLES = {
    "command.deny": "Command blocked",
    "workspace.readonly_block": "Workspace write blocked",
    "session.connect": "SSH session connected",
    "session.disconnect": "SSH session disconnected",
    "system.error": "Gateway system error",
}

_ALLOWED_METADATA_KEYS = {"command_root"}
_MAX_FIELD_LEN = 180


def _clip(value: Any, *, limit: int = _MAX_FIELD_LEN) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _metadata_value(event: Mapping[str, Any], key: str) -> str:
    metadata = event.get("metadata")
    if not isinstance(metadata, Mapping) or key not in _ALLOWED_METADATA_KEYS:
        return ""
    return _clip(metadata.get(key))


def format_audit_event(event: Mapping[str, Any]) -> str | None:
    """Format a metadata-only audit event as a Telegram alert.

    The formatter intentionally avoids raw command text, hostnames, IPs,
    paths, stdout/stderr, and secrets. It only uses already-redacted metadata
    fields that are safe for operator notifications.
    """
    event_type = str(event.get("event_type") or "")
    title = _ALERT_TITLES.get(event_type)
    if not title:
        return None

    lines = [f"[ALERT] <b>{title}</b>", f"type: <code>{event_type}</code>"]

    decision = _clip(event.get("decision"))
    if decision:
        lines.append(f"decision: <code>{decision}</code>")

    command_root = _metadata_value(event, "command_root")
    if command_root:
        lines.append(f"command_root: <code>{command_root}</code>")

    route = _clip(event.get("route"))
    if route:
        lines.append(f"route: <code>{route}</code>")

    profile = _clip(event.get("profile"))
    if profile:
        lines.append(f"profile: <code>{profile}</code>")

    error_code = _clip(event.get("error_code"))
    if error_code:
        lines.append(f"error_code: <code>{error_code}</code>")

    request_id = _clip(event.get("request_id"))
    if request_id:
        lines.append(f"request_id: <code>{request_id}</code>")

    actor_fingerprint = _clip(event.get("actor_fingerprint"))
    if actor_fingerprint:
        lines.append(f"actor: <code>{actor_fingerprint}</code>")

    reason = _clip(event.get("reason"), limit=120)
    if reason:
        lines.append(f"reason: <code>{reason}</code>")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Status rendering for Telegram
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    "ok": "✅",
    "degraded": "⚠️",
    "error": "❌",
    "true": "🟢",
    "false": "🔴",
}


def _status_icon(value: Any) -> str:
    """Map a boolean/string status to an emoji icon."""
    text = str(value).lower()
    return _STATUS_ICONS.get(text, "⚪")


def render_health_status(health: dict[str, Any]) -> str:
    """Render a health dict into Telegram-safe status text.

    Safe: no host/IP/secrets/raw env values.
    Only includes: version, status, ready, redis, postgres,
    persistent_sessions, readonly, mode.
    """
    lines = ["<b>Gateway Status</b>", ""]

    version = str(health.get("version", "unknown"))
    lines.append(f"version: <code>{version}</code>")

    status = str(health.get("status", "unknown"))
    icon = _STATUS_ICONS.get(status, "⚪")
    lines.append(f"status: {icon} <code>{status}</code>")

    ready = health.get("ready")
    if ready is not None:
        lines.append(f"ready: {_status_icon(ready)} <code>{ready}</code>")

    redis = health.get("redis")
    if redis is not None:
        lines.append(f"redis: {_status_icon(redis)} <code>{redis}</code>")

    postgres = health.get("postgres")
    if postgres is not None:
        lines.append(f"postgres: {_status_icon(postgres)} <code>{postgres}</code>")

    persistent = health.get("persistent_sessions")
    if persistent is not None:
        lines.append(f"persistent_sessions: {_status_icon(persistent)} <code>{persistent}</code>")

    readonly = health.get("readonly")
    if readonly is not None:
        lines.append(f"readonly: {_status_icon(readonly)} <code>{readonly}</code>")

    mode = health.get("mode")
    if mode:
        lines.append(f"mode: <code>{mode}</code>")

    return "\n".join(lines)
