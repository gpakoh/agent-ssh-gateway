from __future__ import annotations

from typing import Any

from app.notifier.telegram import TelegramClient


class _FakeResponse:
    status = 200


class _FakePostContext:
    async def __aenter__(self):
        return _FakeResponse()

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakePostContext()


async def test_send_message_passes_proxy_to_aiohttp_post():
    session = _FakeSession()
    client = TelegramClient(
        token="token",
        chat_ids=("chat",),
        dry_run=False,
        proxy="http://proxy.example.invalid:3128",
        session=session,  # type: ignore[arg-type]
    )

    result = await client.send_message("hello")

    assert result[0].ok is True
    assert session.calls[0]["proxy"] == "http://proxy.example.invalid:3128"
    assert session.calls[0]["json"]["text"] == "hello"


async def test_dry_run_does_not_touch_session_even_with_proxy():
    session = _FakeSession()
    client = TelegramClient(
        token="token",
        chat_ids=("chat",),
        dry_run=True,
        proxy="http://proxy.example.invalid:3128",
        session=session,  # type: ignore[arg-type]
    )

    result = await client.send_message("hello")

    assert result[0].dry_run is True
    assert session.calls == []


async def test_dry_run_send_is_logged(caplog):
    """docs/operations/NOTIFIER.md promises dry-run sends are "logged, not
    delivered" — verify that's actually true, not just a doc claim."""
    import logging

    client = TelegramClient(token="token", chat_ids=("chat-1", "chat-2"), dry_run=True)

    with caplog.at_level(logging.INFO, logger="app.notifier.telegram"):
        await client.send_message("hello world")

    assert "notifier_dry_run_send" in caplog.text
    assert "hello world" in caplog.text
