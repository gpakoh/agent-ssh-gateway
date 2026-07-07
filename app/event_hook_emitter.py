"""Event emitter — creates outbox deliveries for matching hooks."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from app import state as _state
from app.event_hook_security import sign_payload
from app.security import redact_secrets

logger = logging.getLogger(__name__)

EVENT_VERSION = 1
SESSION_EVENTS = {"session.connected", "session.disconnected"}
COMMAND_EVENTS = {"command.started", "command.completed", "command.failed"}


def _build_payload(event: str, session_id: str, **extra) -> dict:
    payload = {
        "event": event,
        "event_id": uuid.uuid4().hex,
        "event_version": EVENT_VERSION,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "session_id": session_id,
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


async def emit_event(
    event: str,
    session_id: str,
    host: str = "",
    port: int = 22,
    username: str = "",
    command: str = "",
    exit_code: int | None = None,
    duration: float | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    reason: str = "",
    connected_seconds: float | None = None,
) -> None:
    store = _state.event_hook_store
    if store is None:
        return

    try:
        hooks = await store.find_matching(event, session_id)
    except Exception:
        logger.exception("Failed to query hooks for event %s", event)
        return

    if not hooks:
        return

    for hook in hooks:
        extra = {}
        if event in SESSION_EVENTS:
            extra = {
                "host": host,
                "port": port,
                "username": username,
            }
            if event == "session.disconnected":
                extra["reason"] = reason
                if connected_seconds is not None:
                    extra["connected_seconds"] = round(connected_seconds, 1)

        elif event in COMMAND_EVENTS:
            extra["command"] = command
            if event in ("command.completed", "command.failed"):
                extra["host"] = host
                extra["port"] = port
                extra["username"] = username
                if exit_code is not None:
                    extra["exit_code"] = exit_code
                if duration is not None:
                    extra["duration"] = round(duration, 2)

                if hook.include_output:
                    limit = 65536
                    if stdout:
                        truncated = len(stdout) > limit
                        extra["stdout"] = stdout[:limit]
                        extra["output_truncated"] = truncated
                    if stderr:
                        truncated = len(stderr) > limit
                        extra["stderr"] = stderr[:limit]
                        if not extra.get("output_truncated"):
                            extra["output_truncated"] = truncated

        payload = _build_payload(event, session_id, **extra)
        payload = redact_secrets(payload)
        payload_json = json.dumps(payload, default=str)

        # Sign Payload
        secret = None
        if hook.secret_encrypted and _state.secret_manager:
            try:
                secret = _state.secret_manager.decrypt(hook.secret_encrypted)
            except Exception:
                logger.exception("Failed to decrypt hook secret %s", hook.id)

        timestamp = str(int(datetime.now(UTC).timestamp()))
        signature = sign_payload(secret, payload_json.encode("utf-8"), timestamp)
        delivery_headers = {"Content-Type": "application/json"}
        if signature:
            delivery_headers["X-Webhook-Signature"] = signature
            delivery_headers["X-Webhook-Timestamp"] = timestamp
        delivery_headers["X-Event-Id"] = payload["event_id"]
        delivery_headers["X-Delivery-Id"] = uuid.uuid4().hex

        # Add Custom Headers
        if hook.headers_encrypted and _state.secret_manager:
            try:
                custom = json.loads(_state.secret_manager.decrypt(hook.headers_encrypted))
                if isinstance(custom, dict):
                    delivery_headers.update(custom)
            except Exception:
                logger.exception("Failed to decrypt hook headers %s", hook.id)

        # Enqueue Delivery
        ds = _state.delivery_service
        if ds:
            try:
                hook_id = hook.id
                hook_url = hook.url
                assert hook_id is not None
                assert hook_url is not None
                await ds.enqueue(
                    event_id=payload["event_id"],
                    hook_id=hook_id,
                    event_type=event,
                    url=hook_url,
                    payload_json=payload_json,
                )
            except Exception:
                logger.exception("Failed to enqueue delivery for hook %s", hook.id)
