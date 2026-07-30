"""Telegram callback query handler — writes decisions to gateway admin API."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.notifier.actions import pop_action

logger = logging.getLogger(__name__)

_APPROVAL_API_PATH = "/api/admin/approval/decision"
_ACCESS_API_PATH = "/api/admin/access-control/decision"


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

    # POST to gateway admin API — approval endpoint if approval_id present,
    # otherwise access-control endpoint (existing behavior)
    if gateway_url and gateway_api_key:
        if payload.approval_id:
            api_path = _APPROVAL_API_PATH
            body = {
                "approval_id": payload.approval_id,
                "decision": decision,
                "operator": username,
            }
        else:
            api_path = _ACCESS_API_PATH
            body = {
                "actor_fingerprint": payload.actor_fingerprint,
                "source_ip": payload.source_ip,
                "decision": decision,
                "reason": f"operator:{username}",
            }
        post_result = await _post_decision_to_gateway(
            gateway_url=gateway_url,
            gateway_api_key=gateway_api_key,
            api_path=api_path,
            body=body,
        )
        result["gateway_post"] = post_result
    else:
        result["gateway_post"] = "skipped_no_config"
        logger.warning("callback_skipped_gateway_post: no gateway_url or api_key")

    logger.info(
        "callback_decided: action=%s source_ip=%s by=%s approval_id=%s",
        decision,
        payload.source_ip,
        username,
        payload.approval_id or "",
    )

    # Answer callback query (removes loading spinner)
    if telegram_client is not None:
        await telegram_client.answer_callback_query(callback_query.get("id", ""))

        # Edit message to remove buttons and show decision details
        message = callback_query.get("message", {})
        chat_id = message.get("chat", {}).get("id", "")
        message_id = message.get("message_id")
        if chat_id and message_id is not None:
            label = "✅ Allowed" if decision == "allow" else "❌ Denied"
            fp_short = payload.actor_fingerprint[:12]
            gw_result = result.get("gateway_post", "")
            ttl_line = ""
            if isinstance(gw_result, dict) and "expires_at" in gw_result:
                import time
                ttl_remaining = round(max(0.0, gw_result["expires_at"] - time.time()))
                ttl_line = f"\n⏱ TTL: {ttl_remaining}s"
            elif isinstance(gw_result, str) and gw_result != "ok":
                gw_status = gw_result
                follow_up = f"{label} by @{username}\n⚠ Gateway: {gw_status}\n🔐 {fp_short}… | {payload.source_ip}"
                await telegram_client.edit_message_text(str(chat_id), int(message_id), follow_up)
                return result

            follow_up = f"{label} by @{username}\n🔐 {fp_short}… | {payload.source_ip}{ttl_line}"
            await telegram_client.edit_message_text(str(chat_id), int(message_id), follow_up)

    return result


async def _post_decision_to_gateway(
    *,
    gateway_url: str,
    gateway_api_key: str,
    api_path: str,
    body: dict[str, Any],
) -> str:
    """POST a decision to a gateway admin API endpoint.

    Returns "ok" on success, or error description on failure.
    """
    url = f"{gateway_url.rstrip('/')}{api_path}"
    headers = {"X-API-Key": gateway_api_key}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers) as resp:
                if 200 <= resp.status < 300:
                    return "ok"
                return f"http_{resp.status}"
    except Exception as exc:
        return f"error:{type(exc).__name__}"
