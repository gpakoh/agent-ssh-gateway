"""Adversarial webhook DNS-rebinding tests using aiohttp's real connector path."""

from __future__ import annotations

import asyncio
import socket
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app.event_hook_delivery import (
    DeliveryService,
    _BlockedWebhookDestinationError,
    _ValidatedWebhookResolver,
)
from app.event_hook_security import UrlValidationResult
from app.session_store import WebhookDelivery


async def _scripted_keepalive_server(
    statuses: list[int],
) -> tuple[asyncio.AbstractServer, int, list[bytes], list[int]]:
    hits: list[bytes] = []
    connections: list[int] = []
    remaining = list(statuses)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(1)
        try:
            while remaining:
                try:
                    request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break
                hits.append(request)
                status = remaining.pop(0)
                reason = "No Content" if status == 204 else "Internal Server Error"
                writer.write(
                    f"HTTP/1.1 {status} {reason}\r\n"
                    "Content-Length: 0\r\n"
                    "Connection: keep-alive\r\n\r\n".encode()
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, hits, connections


async def _start_delivery_service(instance_id: str) -> DeliveryService:
    ds = DeliveryService("sqlite+aiosqlite:///:memory:", instance_id)
    await ds.create_tables()
    await ds.start(
        poll_interval=60.0,
        connect_timeout=1.0,
        read_timeout=1.0,
        max_attempts=3,
        retry_base_sec=0.01,
        retry_max_sec=0.01,
        lease_ttl=30.0,
        retention_sent_days=7,
        retention_dead_days=30,
    )
    return ds


async def _load_delivery(ds: DeliveryService, delivery_id: str) -> WebhookDelivery:
    async with ds._session_factory() as session:
        result = await session.execute(
            select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_mixed_public_private_dns_answer_fails_closed(monkeypatch):
    loop = asyncio.get_running_loop()
    real_getaddrinfo = loop.getaddrinfo
    dns_calls = 0

    async def _mixed_getaddrinfo(host, port, *args, **kwargs):
        nonlocal dns_calls
        if host == "mixed.test":
            dns_calls += 1
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.11", port)),
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
            ]
        return await real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", _mixed_getaddrinfo)
    resolver = _ValidatedWebhookResolver()
    try:
        with pytest.raises(_BlockedWebhookDestinationError, match="Blocked destination"):
            await resolver.resolve("mixed.test", 443)
    finally:
        await resolver.close()

    assert dns_calls == 1


@pytest.mark.asyncio
async def test_literal_loopback_is_rejected_before_aiohttp_connects():
    server, port, hits, _connections = await _scripted_keepalive_server([204])
    ds = await _start_delivery_service("literal-loopback")
    try:
        delivery_id = await ds.enqueue(
            "evt-literal",
            "hook-literal",
            "command.completed",
            f"http://127.0.0.1:{port}/hook",
            "{}",
        )
        delivery = await _load_delivery(ds, delivery_id)
        await ds._send_delivery(delivery, 1, 0.01, 0.01)
        record = await ds._get_record(delivery_id)

        assert hits == []
        assert record is not None
        assert record.status == "dead"
        assert "Blocked destination" in (record.last_error or "")
    finally:
        await ds.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ipv6_loopback_is_rejected_and_public_ipv6_is_preserved(monkeypatch):
    loop = asyncio.get_running_loop()
    real_getaddrinfo = loop.getaddrinfo

    async def _ipv6_getaddrinfo(host, port, *args, **kwargs):
        if host == "blocked-v6.test":
            return [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("::1", port, 0, 0),
                )
            ]
        if host == "public-v6.test":
            return [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("2606:4700:4700::1111", port, 0, 0),
                )
            ]
        return await real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", _ipv6_getaddrinfo)
    resolver = _ValidatedWebhookResolver()
    try:
        with pytest.raises(_BlockedWebhookDestinationError, match="Blocked destination"):
            await resolver.resolve("blocked-v6.test", 443, family=socket.AF_INET6)
        resolved = await resolver.resolve("public-v6.test", 443, family=socket.AF_INET6)
    finally:
        await resolver.close()

    assert [item["host"] for item in resolved] == ["2606:4700:4700::1111"]


@pytest.mark.asyncio
async def test_concurrent_hostnames_keep_resolution_results_isolated(monkeypatch):
    loop = asyncio.get_running_loop()
    real_getaddrinfo = loop.getaddrinfo
    entered = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []

    async def _per_host_getaddrinfo(host, port, *args, **kwargs):
        if host in {"a.test", "b.test"}:
            seen.append(host)
            if len(seen) == 2:
                entered.set()
            await release.wait()
            ip = "203.0.113.21" if host == "a.test" else "203.0.113.22"
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
            ]
        return await real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", _per_host_getaddrinfo)
    resolver = _ValidatedWebhookResolver()
    try:
        tasks = [
            asyncio.create_task(resolver.resolve("a.test", 443)),
            asyncio.create_task(resolver.resolve("b.test", 443)),
        ]
        await asyncio.wait_for(entered.wait(), timeout=1)
        release.set()
        a_result, b_result = await asyncio.gather(*tasks)
    finally:
        await resolver.close()

    assert a_result[0]["hostname"] == "a.test"
    assert a_result[0]["host"] == "203.0.113.21"
    assert b_result[0]["hostname"] == "b.test"
    assert b_result[0]["host"] == "203.0.113.22"


@pytest.mark.asyncio
async def test_retry_resolves_fresh_and_does_not_reuse_connection_or_dns_cache(monkeypatch):
    server, port, hits, connections = await _scripted_keepalive_server([500, 204])
    loop = asyncio.get_running_loop()
    real_getaddrinfo = loop.getaddrinfo
    dns_calls = 0

    async def _local_getaddrinfo(host, requested_port, *args, **kwargs):
        nonlocal dns_calls
        if host == "retry.test":
            dns_calls += 1
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", requested_port),
                )
            ]
        return await real_getaddrinfo(host, requested_port, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", _local_getaddrinfo)
    monkeypatch.setattr(
        "app.event_hook_delivery.validate_destination_ip",
        lambda _ip: UrlValidationResult(True),
    )

    ds = await _start_delivery_service("retry-test")
    try:
        delivery_id = await ds.enqueue(
            "evt-retry",
            "hook-retry",
            "command.completed",
            f"http://retry.test:{port}/hook",
            "{}",
        )
        first = await _load_delivery(ds, delivery_id)
        await ds._send_delivery(first, 3, 0.01, 0.01)
        after_first = await ds._get_record(delivery_id)
        assert after_first is not None
        assert after_first.status == "failed"

        await ds._send_delivery(after_first, 3, 0.01, 0.01)
        after_second = await ds._get_record(delivery_id)

        assert after_second is not None
        assert after_second.status == "sent"
        assert dns_calls == 2
        assert len(hits) == 2
        assert len(connections) == 2
    finally:
        await ds.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_delivery_connector_keeps_tls_verification_and_original_host_for_sni(
    monkeypatch,
    tmp_path: Path,
):
    hostname = "tls-hook.test"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ssl.load_cert_chain(certfile=cert_path, keyfile=key_path)
    client_ssl = ssl.create_default_context(cafile=str(cert_path))
    assert client_ssl.verify_mode == ssl.CERT_REQUIRED
    assert client_ssl.check_hostname is True

    requests: list[bytes] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            requests.append(request)
            writer.write(
                b"HTTP/1.1 204 No Content\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0, ssl=server_ssl)
    port = server.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()
    real_getaddrinfo = loop.getaddrinfo

    async def _tls_getaddrinfo(host, requested_port, *args, **kwargs):
        if host == hostname:
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", requested_port),
                )
            ]
        return await real_getaddrinfo(host, requested_port, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", _tls_getaddrinfo)
    monkeypatch.setattr(
        "app.event_hook_delivery.validate_destination_ip",
        lambda _ip: UrlValidationResult(True),
    )

    connector = aiohttp.TCPConnector(
        ssl=client_ssl,
        resolver=_ValidatedWebhookResolver(),
        use_dns_cache=False,
        force_close=True,
    )
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(f"https://{hostname}:{port}/hook", data=b"{}") as response:
                assert response.status == 204
    finally:
        server.close()
        await server.wait_closed()

    request_text = requests[0].decode("latin-1")
    assert f"Host: {hostname}:{port}\r\n" in request_text


@pytest.mark.asyncio
async def test_delivery_service_connector_security_options_are_explicit():
    ds = await _start_delivery_service("connector-options")
    try:
        connector = ds._http_session.connector
        assert isinstance(connector, aiohttp.TCPConnector)
        assert isinstance(connector._resolver, _ValidatedWebhookResolver)
        assert connector._use_dns_cache is False
        assert connector.force_close is True
        assert connector._ssl is True
    finally:
        await ds.close()
