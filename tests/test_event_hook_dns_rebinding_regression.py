"""Regression for connect-time webhook DNS rebinding."""

import asyncio
import socket

import pytest
from sqlalchemy import select

from app.event_hook_delivery import DeliveryService
from app.session_store import WebhookDelivery


@pytest.mark.asyncio
async def test_dns_rebinding_cannot_change_checked_public_ip_to_loopback(monkeypatch):
    """A security lookup may not be followed by a second connect-time lookup."""
    hits: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            hits.append(request)
            writer.write(
                b"HTTP/1.1 204 No Content\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()
    real_getaddrinfo = loop.getaddrinfo
    dns_calls: list[str] = []

    async def rebind(host, requested_port, *args, **kwargs):
        if host == "rebind.test":
            dns_calls.append(host)
            ip = "203.0.113.10" if len(dns_calls) == 1 else "127.0.0.1"
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (ip, requested_port),
                )
            ]
        return await real_getaddrinfo(host, requested_port, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", rebind)
    ds = DeliveryService("sqlite+aiosqlite:///:memory:", "dns-rebind-regression")
    await ds.create_tables()
    await ds.start(
        poll_interval=60.0,
        connect_timeout=1.0,
        read_timeout=1.0,
        max_attempts=1,
        retry_base_sec=0.01,
        retry_max_sec=0.01,
        lease_ttl=30.0,
        retention_sent_days=7,
        retention_dead_days=30,
    )
    try:
        delivery_id = await ds.enqueue(
            "evt-rebind",
            "hook-rebind",
            "command.completed",
            f"http://rebind.test:{port}/hook",
            "{}",
        )
        async with ds._session_factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
            )
            delivery = result.scalar_one()

        await ds._send_delivery(delivery, 1, 0.01, 0.01)
        record = await ds._get_record(delivery_id)

        assert hits == []
        assert len(dns_calls) == 1
        assert record is not None
        assert record.status != "sent"
    finally:
        await ds.close()
        server.close()
        await server.wait_closed()
