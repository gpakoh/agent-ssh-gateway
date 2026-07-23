"""Telegram getUpdates polling for callback queries."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class CallbackPoller:
    """Polls Telegram getUpdates for inline button callback queries."""

    def __init__(
        self,
        token: str | None = None,
        *,
        api_base: str = "https://api.telegram.org",
        proxy: str | None = None,
        handle_callback_fn: Any = None,
        interval_seconds: float = 2.0,
    ) -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._proxy = proxy
        self._handle_callback = handle_callback_fn
        self._interval = interval_seconds
        self._offset: int = 0
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def run_forever(self) -> None:
        """Run the polling loop until cancelled."""
        self._running = True
        try:
            while True:
                try:
                    await self.poll_once()
                except Exception:
                    logger.warning("callback_poller_failed", exc_info=True)
                await asyncio.sleep(self._interval)
        finally:
            self._running = False

    async def poll_once(self, session: Any = None) -> list[dict[str, Any]]:
        """Poll for updates once. Returns list of callback results."""
        if not self._token:
            return []

        import aiohttp

        use_external = session is not None
        if not use_external:
            session = aiohttp.ClientSession()

        url = f"{self._api_base}/bot{self._token}/getUpdates"
        params: dict[str, Any] = {"offset": self._offset, "timeout": 0}
        results: list[dict[str, Any]] = []

        try:
            async with session.get(url, params=params, proxy=self._proxy) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                if not data.get("ok"):
                    return []

                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    cb = update.get("callback_query")
                    if cb and self._handle_callback:
                        result = await self._handle_callback(cb)
                        results.append(result)
        except Exception as exc:
            logger.warning("getUpdates failed: %s", exc)
        finally:
            if not use_external:
                await session.close()

        return results
