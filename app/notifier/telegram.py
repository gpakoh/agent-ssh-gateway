"""Telegram delivery client for gateway notifier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass(frozen=True)
class TelegramSendResult:
    """Result of one Telegram send attempt."""

    chat_id: str
    ok: bool
    status: int = 0
    dry_run: bool = False
    error: str = ""


class TelegramClient:
    """Minimal Telegram Bot API client.

    The token is never logged or exposed in repr output. Tests can inject an
    aiohttp-compatible session to avoid network calls.
    """

    def __init__(
        self,
        *,
        token: str,
        chat_ids: tuple[str, ...],
        dry_run: bool = True,
        timeout_seconds: float = 10.0,
        api_base: str = "https://api.telegram.org",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._token = token
        self._chat_ids = chat_ids
        self._dry_run = dry_run
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._api_base = api_base.rstrip("/")
        self._session = session
        self._owns_session = session is None

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def send_message(self, text: str) -> list[TelegramSendResult]:
        """Send a message to all configured chats.

        Dry-run mode reports success without touching the network.
        """
        if self._dry_run:
            return [TelegramSendResult(chat_id=chat_id, ok=True, dry_run=True) for chat_id in self._chat_ids]

        if not self._token or not self._chat_ids:
            return [TelegramSendResult(chat_id="", ok=False, error="telegram_not_configured")]

        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

        url = f"{self._api_base}/bot{self._token}/sendMessage"
        results: list[TelegramSendResult] = []
        for chat_id in self._chat_ids:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            try:
                async with self._session.post(url, json=payload) as response:
                    ok = 200 <= response.status < 300
                    results.append(TelegramSendResult(chat_id=chat_id, ok=ok, status=response.status))
            except Exception as exc:
                results.append(
                    TelegramSendResult(chat_id=chat_id, ok=False, error=type(exc).__name__)
                )
        return results
