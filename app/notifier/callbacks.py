"""Telegram callback query handler — writes decisions to gateway admin API."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.notifier.actions import pop_action

logger = logging.getLogger(__name__)


async def handle_callback_query(
    callback_query: dict[str, Any],
    *,
    gateway_url: str = "",
    gateway_api_key: str = "",
    telegram_client: Any = None,
) -> dict[str, Any]:
    """Process a Telegram callback query from an inline button.

    Posts the decision to the gateway admin API, answers the callback query,
    and edits the original message to remove buttons.

    Returns a result dict with action_taken and message fields.
    """
    data = callback_query.get("data", "")
    from_user = callback_query.get("from", {})

    if not data:
        return {"action_taken": False, "reason": "no_data"}

    payload = pop_action(data)
    if payload is None:
        return {"action_taken": False, "reason": "invalid_or_expired_token"}

    decision_map = {"allow_actor": "allow", "deny_actor": "deny"}
    decision = decision_map.get(payload.action_type, payload.action_type)
    username = from_user.get("username", "unknown")

    result: dict[str, Any] = {
        "action_taken": True,
        "decision": decision,
        "source_ip": payload.source_ip,
        "actor_fingerprint": payload.actor_fingerprint,
        "decided_by": username,
    }

    # POST to gateway admin API
    if gateway_url and gateway_api_key:
        post_result = await _post_decision_to_gateway(
            gateway_url=gateway_url,
            gateway_api_key=gateway_api_key,
            actor_fingerprint=payload.actor_fingerprint,
            source_ip=payload.source_ip,
            decision=decision,
            reason=f"operator:{username}",
        )
        result["gateway_post"] = post_result
    else:
        result["gateway_post"] = "skipped_no_config"
        logger.warning("callback_skipped_gateway_post: no gateway_url or api_key")

    logger.info(
        "callback_decided: action=%s source_ip=%s by=%s",
        decision,
        payload.source_ip,
        username,
    )

    # Answer callback query (removes loading spinner)
    if telegram_client is not None:
        await telegram_client.answer_callback_query(callback_query.get("id", ""))

        # Edit message to remove buttons
        message = callback_query.get("message", {})
        chat_id = message.get("chat", {}).get("id", "")
        message_id = message.get("message_id")
        if chat_id and message_id is not None:
            label = "Allowed" if decision == "allow" else "Denied"
            follow_up = f"<b>{label}</b> by @{username}"
            await telegram_client.edit_message_text(str(chat_id), int(message_id), follow_up)

    return result


async def _post_decision_to_gateway(
    *,
    gateway_url: str,
    gateway_api_key: str,
    actor_fingerprint: str,
    source_ip: str,
    decision: str,
    reason: str,
) -> str:
    """POST access-control decision to gateway admin API.

    Returns "ok" on success, or error description on failure.
    """
    url = f"{gateway_url.rstrip('/')}/api/admin/access-control/decision"
    body = {
        "actor_fingerprint": actor_fingerprint,
        "source_ip": source_ip,
        "decision": decision,
        "reason": reason,
    }
    headers = {"X-API-Key": gateway_api_key}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers) as resp:
                if 200 <= resp.status < 300:
                    return "ok"
                return f"http_{resp.status}"
    except Exception as exc:
        return f"error:{type(exc).__name__}"
