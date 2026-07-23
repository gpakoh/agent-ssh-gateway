"""Tests for Telegram inline buttons and callback gateway integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.notifier.actions import clear_actions, create_action
from app.notifier.callbacks import handle_callback_query
from app.notifier.config import NotifierSettings
from app.notifier.service import GatewayNotifierService


@pytest.fixture(autouse=True)
def _clean():
    clear_actions()
    yield
    clear_actions()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    event_type: str = "command.deny",
    actor_fingerprint: str = "fp-abc",
    source_ip: str = "192.168.1.50",
    event_id: str = "evt-1",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "actor_fingerprint": actor_fingerprint,
        "source_ip": source_ip,
        "message": "test event",
    }


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.answered: list[str] = []
        self.edited: list[tuple[str, int, str]] = []

    async def send_message(self, text: str, *, reply_markup: Any = None) -> list[Any]:
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return []

    async def answer_callback_query(self, callback_query_id: str) -> bool:
        self.answered.append(callback_query_id)
        return True

    async def edit_message_text(self, chat_id: str, message_id: int, text: str) -> bool:
        self.edited.append((chat_id, message_id, text))
        return True

    async def close(self) -> None:
        pass


class FakeGateway:
    def __init__(self) -> None:
        self.health_status: dict[str, Any] = {"status": "ok", "ready": True}

    async def health(self) -> dict[str, Any]:
        return self.health_status

    async def recent_events(self, *, limit: int = 10, event_type: str = "") -> list[dict]:
        return []

    async def close(self) -> None:
        pass


def _make_service(*, action_event_types: tuple[str, ...] = ("command.deny", "workspace.readonly_block")) -> tuple[GatewayNotifierService, FakeTelegram, FakeGateway]:
    settings = NotifierSettings(
        enabled=True,
        dry_run=False,
        gateway_url="http://gateway:8085",
        gateway_api_key="test-key",
        action_event_types=action_event_types,
        event_types=("command.deny", "workspace.readonly_block", "session.connect"),
        realtime_event_types=("command.deny", "workspace.readonly_block"),
        digest_types=(),
        max_alerts_per_poll=10,
    )
    tg = FakeTelegram()
    gw = FakeGateway()
    svc = GatewayNotifierService(settings=settings, gateway=gw, telegram=tg)
    return svc, tg, gw


# ---------------------------------------------------------------------------
# Tests — buttons attached to action events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_buttons_attach_to_command_deny():
    svc, tg, gw = _make_service()
    gw.recent_events = AsyncMock(return_value=[_make_event(event_type="command.deny")])

    count = await svc.poll_once()
    assert count == 1
    assert len(tg.sent) == 1
    markup = tg.sent[0]["reply_markup"]
    assert markup is not None
    assert "inline_keyboard" in markup
    row = markup["inline_keyboard"][0]
    assert len(row) == 2
    assert row[0]["text"] == "Allow"
    assert row[1]["text"] == "Deny"


@pytest.mark.asyncio()
async def test_buttons_attach_to_workspace_readonly_block():
    svc, tg, gw = _make_service()
    gw.recent_events = AsyncMock(
        return_value=[_make_event(event_type="workspace.readonly_block", event_id="evt-ro")]
    )

    count = await svc.poll_once()
    assert count == 1
    markup = tg.sent[0]["reply_markup"]
    assert markup is not None
    assert "inline_keyboard" in markup


# ---------------------------------------------------------------------------
# Tests — no buttons when actor or source_ip missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_no_buttons_when_actor_absent():
    svc, tg, gw = _make_service()
    event = _make_event()
    del event["actor_fingerprint"]
    gw.recent_events = AsyncMock(return_value=[event])

    count = await svc.poll_once()
    assert count == 1
    assert tg.sent[0]["reply_markup"] is None


@pytest.mark.asyncio()
async def test_no_buttons_when_source_ip_absent():
    svc, tg, gw = _make_service()
    event = _make_event()
    del event["source_ip"]
    gw.recent_events = AsyncMock(return_value=[event])

    count = await svc.poll_once()
    assert count == 1
    assert tg.sent[0]["reply_markup"] is None


@pytest.mark.asyncio()
async def test_no_buttons_for_digest_events_by_default():
    """Digest events never get buttons even if they have actor+source_ip."""
    svc, tg, gw = _make_service()
    # session.connect is a digest type by default
    gw.recent_events = AsyncMock(
        return_value=[_make_event(event_type="session.connect", event_id="evt-digest")]
    )

    count = await svc.poll_once()
    assert count == 0
    assert len(tg.sent) == 0


# ---------------------------------------------------------------------------
# Tests — callback posts to gateway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_callback_posts_correct_gateway_payload():
    """Callback sends correct JSON body to gateway admin API."""
    token = create_action(
        action_type="allow_actor",
        actor_fingerprint="fp-test",
        source_ip="10.0.0.5",
        event_type="command.deny",
        request_id="req-1",
    )

    posted: list[dict[str, Any]] = []

    async def fake_post(*, gateway_url, gateway_api_key, actor_fingerprint, source_ip, decision, reason):
        posted.append({
            "gateway_url": gateway_url,
            "gateway_api_key": gateway_api_key,
            "actor_fingerprint": actor_fingerprint,
            "source_ip": source_ip,
            "decision": decision,
            "reason": reason,
        })
        return "ok"

    with patch("app.notifier.callbacks._post_decision_to_gateway", new=fake_post):
        result = await handle_callback_query(
            {"id": "cb-1", "data": token, "from": {"username": "op"}},
            gateway_url="http://gw:8085",
            gateway_api_key="key-123",
        )

    assert result["action_taken"] is True
    assert result["decision"] == "allow"
    assert len(posted) == 1
    assert posted[0]["actor_fingerprint"] == "fp-test"
    assert posted[0]["source_ip"] == "10.0.0.5"
    assert posted[0]["decision"] == "allow"
    assert posted[0]["reason"] == "operator:op"
    assert posted[0]["gateway_url"] == "http://gw:8085"
    assert posted[0]["gateway_api_key"] == "key-123"


@pytest.mark.asyncio()
async def test_callback_sends_x_api_key_header():
    """_post_decision_to_gateway sends X-API-Key header."""
    import aiohttp

    captured_headers: dict[str, str] = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, *, json, headers):
            captured_headers.update(headers)
            return FakeResponse()

    class PatchedSession(FakeSession):
        pass

    with patch.object(aiohttp, "ClientSession", PatchedSession):
        from app.notifier.callbacks import _post_decision_to_gateway

        result = await _post_decision_to_gateway(
            gateway_url="http://gw:8085",
            gateway_api_key="secret-key",
            actor_fingerprint="fp",
            source_ip="1.2.3.4",
            decision="deny",
            reason="operator:admin",
        )

    assert result == "ok"
    assert captured_headers.get("X-API-Key") == "secret-key"


@pytest.mark.asyncio()
async def test_expired_token_does_not_call_gateway():
    """Expired token returns invalid_or_expired_token without calling gateway."""
    token = create_action(
        action_type="deny_actor",
        actor_fingerprint="fp-exp",
        source_ip="10.0.0.99",
        event_type="command.deny",
        request_id="req-exp",
        ttl_seconds=0,
    )
    import time
    time.sleep(0.01)

    result = await handle_callback_query(
        {"id": "cb-exp", "data": token, "from": {"username": "op"}},
        gateway_url="http://gw:8085",
        gateway_api_key="key",
    )

    assert result["action_taken"] is False
    assert result["reason"] == "invalid_or_expired_token"


@pytest.mark.asyncio()
async def test_answer_callback_query_called():
    """answer_callback_query is called after processing."""
    token = create_action(
        action_type="allow_actor",
        actor_fingerprint="fp-ans",
        source_ip="10.0.0.1",
        event_type="command.deny",
        request_id="req-ans",
    )
    tg = FakeTelegram()

    with patch("app.notifier.callbacks._post_decision_to_gateway", return_value="ok"):
        await handle_callback_query(
            {"id": "cb-ans", "data": token, "from": {"username": "op"}},
            gateway_url="http://gw:8085",
            gateway_api_key="key",
            telegram_client=tg,
        )

    assert "cb-ans" in tg.answered


@pytest.mark.asyncio()
async def test_edit_message_text_removes_buttons():
    """edit_message_text is called to remove inline buttons."""
    token = create_action(
        action_type="deny_actor",
        actor_fingerprint="fp-edit",
        source_ip="10.0.0.2",
        event_type="workspace.readonly_block",
        request_id="req-edit",
    )
    tg = FakeTelegram()

    with patch("app.notifier.callbacks._post_decision_to_gateway", return_value="ok"):
        await handle_callback_query(
            {
                "id": "cb-edit",
                "data": token,
                "from": {"username": "admin"},
                "message": {
                    "chat": {"id": "12345"},
                    "message_id": 42,
                },
            },
            gateway_url="http://gw:8085",
            gateway_api_key="key",
            telegram_client=tg,
        )

    assert len(tg.edited) == 1
    chat_id, msg_id, text = tg.edited[0]
    assert chat_id == "12345"
    assert msg_id == 42
    assert "Denied" in text
    assert "admin" in text


@pytest.mark.asyncio()
async def test_dry_run_does_not_start_poller():
    """Dry-run mode does not configure CallbackPoller (can_send_telegram=False)."""
    settings = NotifierSettings(
        enabled=True,
        dry_run=True,
        telegram_token="fake-token",
        telegram_chat_ids=("123",),
        gateway_url="http://gw:8085",
        gateway_api_key="key",
    )
    assert settings.can_send_telegram is False
