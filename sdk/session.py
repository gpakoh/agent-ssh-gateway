"""High-level SSH Gateway session context managers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from examples.mcp_server.gateway_client import GatewayClient


class GatewaySession:
    """Synchronous context manager for SSH Gateway.

    Usage::

        with GatewaySession(client) as gw:
            result = gw.run("ls -la")
    """

    def __init__(self, client: GatewayClient) -> None:
        self.client = client
        self.session_id: str | None = None

    def __enter__(self) -> GatewaySession:
        self.session_id = self.client.connect()
        return self

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        self._disconnect_best_effort()

    def _disconnect_best_effort(self) -> None:
        if self.session_id:
            try:
                self.client.disconnect(self.session_id)
            except Exception:
                pass
