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

    def run(self, command: str, timeout: int | None = None) -> dict:
        """Execute command and wait for completion. Returns job result dict."""
        job = self.client.execute_restricted(
            session_id=self.session_id, command=command
        )
        return self.client.wait_job(
            job_id=job["job_id"], timeout=timeout
        )

    def read(self, path: str) -> str:
        """Read file content from remote host."""
        result = self.client.read_file(session_id=self.session_id, path=path)
        return result.get("content", "")

    def write(self, path: str, content: str) -> dict:
        """Write file. Returns raw Gateway response — may contain pending_confirmation."""
        return self.client.write_file(
            session_id=self.session_id, path=path, content=content
        )

    def session_health(self) -> dict:
        """Check SSH session health."""
        return self.client.session_health(session_id=self.session_id)


class AsyncGatewaySession:
    """Async context manager for SSH Gateway.

    Usage::

        async with AsyncGatewaySession(client) as gw:
            result = await gw.run("ls -la")
    """

    def __init__(self, client: GatewayClient) -> None:
        self.client = client
        self.session_id: str | None = None

    async def __aenter__(self) -> AsyncGatewaySession:
        self.session_id = await self.client.connect()
        return self

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        await self._disconnect_best_effort()

    async def _disconnect_best_effort(self) -> None:
        if self.session_id:
            try:
                await self.client.disconnect(self.session_id)
            except Exception:
                pass

    async def run(self, command: str, timeout: int | None = None) -> dict:
        """Execute command and wait for completion. Returns job result dict."""
        job = await self.client.execute_restricted(
            session_id=self.session_id, command=command
        )
        return await self.client.wait_job(
            job_id=job["job_id"], timeout=timeout
        )

    async def read(self, path: str) -> str:
        """Read file content from remote host."""
        result = await self.client.read_file(session_id=self.session_id, path=path)
        return result.get("content", "")

    async def write(self, path: str, content: str) -> dict:
        """Write file. Returns raw Gateway response — may contain pending_confirmation."""
        return await self.client.write_file(
            session_id=self.session_id, path=path, content=content
        )

    async def session_health(self) -> dict:
        """Check SSH session health."""
        return await self.client.session_health(session_id=self.session_id)
