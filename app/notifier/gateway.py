"""Gateway API client for notifier polling."""

from __future__ import annotations

from typing import Any

import aiohttp


class GatewayHealthError(Exception):
    """Raised when the gateway health endpoint returns non-2xx."""

    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(f"health check failed: {status} {reason}")


class GatewayAuditClient:
    """Read-only client for gateway health and audit endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 10.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session = session
        self._owns_session = session is None

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def health(self) -> dict[str, Any]:
        """Poll GET /health and return parsed JSON.

        Returns the response body on 2xx.  Raises ``GatewayHealthError`` on
        non-2xx — the exception carries status code and reason only, never
        the API key or full response body.
        """
        if not self._api_key:
            raise RuntimeError("GATEWAY_NOTIFIER_API_KEY is required for health check")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

        headers = {"X-API-Key": self._api_key}
        async with self._session.get(
            f"{self._base_url}/health",
            headers=headers,
        ) as response:
            if response.status != 200:
                raise GatewayHealthError(response.status, response.reason or "Unknown")
            return await response.json()

    async def recent_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent audit events newest-first from the gateway."""
        if not self._api_key:
            raise RuntimeError("GATEWAY_NOTIFIER_API_KEY is required to poll audit events")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

        params = {"limit": str(limit), "sort": "newest"}
        headers = {"X-API-Key": self._api_key}
        async with self._session.get(
            f"{self._base_url}/api/admin/audit/recent",
            headers=headers,
            params=params,
        ) as response:
            response.raise_for_status()
            payload = await response.json()

        events = payload.get("events", [])
        if not isinstance(events, list):
            return []
        return [event for event in events if isinstance(event, dict)]
