"""Tests for Telegram callback query handling (gateway API integration)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from app.notifier.actions import clear_actions, create_action
from app.notifier.callbacks import handle_callback_query


@pytest.fixture(autouse=True)
def _clean():
    clear_actions()
    yield
    clear_actions()


class FakeTelegram:
    def __init__(self) -> None:
        self.answered: list[str] = []
        self.edited: list[tuple[str, int, str]] = []

    async def answer_callback_query(self, callback_query_id: str) -> bool:
        self.answered.append(callback_query_id)
        return True

    async def edit_message_text(self, chat_id: str, message_id: int, text: str) -> bool:
        self.edited.append((chat_id, message_id, text))
        return True


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Tests — allow decision
# ---------------------------------------------------------------------------


def test_handle_callback_posts_allow_to_gateway():
    token = create_action(
        action_type="allow_actor",
        actor_fingerprint="fingerprint-abc",
        source_ip="192.168.1.50",
        event_type="session.connect",
        request_id="req-test",
    )

    posted: list[dict[str, Any]] = []

    async def fake_post(**kwargs):
        posted.append(kwargs)
        return "ok"

    with patch("app.notifier.callbacks._post_decision_to_gateway", new=fake_post):
        result = _run(handle_callback_query(
            {"id": "cb-1", "data": token, "from": {"username": "operator"}},
            gateway_url="http://gw:8085",
            gateway_api_key="key-1",
        ))

    assert result["action_taken"] is True
    assert result["decision"] == "allow"
    assert len(posted) == 1
    assert posted[0]["decision"] == "allow"
    assert posted[0]["actor_fingerprint"] == "fingerprint-abc"
    assert posted[0]["source_ip"] == "192.168.1.50"
    assert posted[0]["reason"] == "operator:operator"


# ---------------------------------------------------------------------------
# Tests — deny decision
# ---------------------------------------------------------------------------


def test_handle_callback_posts_deny_to_gateway():
    token = create_action(
        action_type="deny_actor",
        actor_fingerprint="bad-actor",
        source_ip="10.0.0.99",
        event_type="session.connect",
        request_id="req-deny",
    )

    posted: list[dict[str, Any]] = []

    async def fake_post(**kwargs):
        posted.append(kwargs)
        return "ok"

    with patch("app.notifier.callbacks._post_decision_to_gateway", new=fake_post):
        result = _run(handle_callback_query(
            {"id": "cb-4", "data": token, "from": {"username": "admin"}},
            gateway_url="http://gw:8085",
            gateway_api_key="key-2",
        ))

    assert result["action_taken"] is True
    assert result["decision"] == "deny"
    assert len(posted) == 1
    assert posted[0]["decision"] == "deny"


# ---------------------------------------------------------------------------
# Tests — error cases
# ---------------------------------------------------------------------------


def test_handle_callback_returns_invalid_token_for_unknown():
    result = _run(handle_callback_query(
        {"id": "cb-2", "data": "bogus-token", "from": {"username": "op"}},
        gateway_url="http://gw:8085",
        gateway_api_key="key",
    ))
    assert result["action_taken"] is False
    assert result["reason"] == "invalid_or_expired_token"


def test_handle_callback_returns_no_data_for_empty():
    result = _run(handle_callback_query(
        {"id": "cb-3", "data": "", "from": {}},
        gateway_url="http://gw:8085",
        gateway_api_key="key",
    ))
    assert result["action_taken"] is False
    assert result["reason"] == "no_data"


# ---------------------------------------------------------------------------
# Tests — Telegram client calls
# ---------------------------------------------------------------------------


def test_handle_callback_calls_answer_and_edit():
    token = create_action(
        action_type="allow_actor",
        actor_fingerprint="fp-ae",
        source_ip="10.0.0.3",
        event_type="command.deny",
        request_id="req-ae",
    )
    tg = FakeTelegram()

    with patch("app.notifier.callbacks._post_decision_to_gateway", return_value="ok"):
        _run(handle_callback_query(
            {
                "id": "cb-ae",
                "data": token,
                "from": {"username": "op"},
                "message": {"chat": {"id": "99"}, "message_id": 7},
            },
            gateway_url="http://gw:8085",
            gateway_api_key="key",
            telegram_client=tg,
        ))

    assert "cb-ae" in tg.answered
    assert len(tg.edited) == 1
    assert tg.edited[0] == ("99", 7, "<b>Allowed</b> by @op")


# ---------------------------------------------------------------------------
# Tests — CallbackPoller
# ---------------------------------------------------------------------------


def test_poll_once_no_token_returns_empty():
    from app.notifier.get_updates import CallbackPoller
    poller = CallbackPoller(token=None)
    results = _run(poller.poll_once())
    assert results == []
