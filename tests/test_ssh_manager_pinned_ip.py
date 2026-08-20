"""Tests for create_session()'s pinned_ip — the DNS-rebinding fix.

validate_target_host() resolves the target hostname and checks the
resolved IP(s) against the allowed/denied CIDR policy. Without pinned_ip,
paramiko's own client.connect(hostname=host) resolves `host` AGAIN,
independently, moments later — a second DNS lookup an attacker
controlling a low-TTL record can answer differently from the first,
returning an allowed IP for the policy check and an internal/metadata IP
for the real connection. pinned_ip closes that gap: the caller (router)
passes the exact IP validate_target_host already approved, and
create_session dials that IP directly via a raw socket instead of letting
paramiko re-resolve `host`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.ssh_manager import SSHSessionManager


def _mock_client():
    client = MagicMock()
    client.get_transport.return_value = None
    return client


@pytest.mark.asyncio
async def test_pinned_ip_dials_the_exact_ip_not_the_hostname():
    manager = SSHSessionManager(cleanup_interval=3600)
    try:
        client = _mock_client()
        fake_socket = MagicMock(name="fake_connected_socket")
        fake_socket.getpeername.return_value = ("203.0.113.5", 22)
        with (
            patch("app.ssh_manager.paramiko.SSHClient", return_value=client),
            patch(
                "app.ssh_manager.socket.create_connection", return_value=fake_socket
            ) as mock_create_connection,
        ):
            await manager.create_session(
                host="attacker-controlled.example.com",
                port=22,
                username="root",
                password="x",
                pinned_ip="203.0.113.5",
            )

            mock_create_connection.assert_called_once_with(
                ("203.0.113.5", 22), timeout=30
            )
            connect_kwargs = client.connect.call_args.kwargs
            assert connect_kwargs["sock"] is fake_socket
            # hostname stays the original name — host-key/known-hosts
            # lookups must still key off what the operator recognizes.
            assert connect_kwargs["hostname"] == "attacker-controlled.example.com"
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_without_pinned_ip_no_raw_socket_is_created():
    """Backward compat: omitting pinned_ip must behave exactly as before —
    paramiko does its own resolution and connection (sock=None)."""
    manager = SSHSessionManager(cleanup_interval=3600)
    try:
        client = _mock_client()
        with (
            patch("app.ssh_manager.paramiko.SSHClient", return_value=client),
            patch("app.ssh_manager.socket.create_connection") as mock_create_connection,
        ):
            await manager.create_session(
                host="good-host", port=22, username="root", password="x"
            )
            mock_create_connection.assert_not_called()
            assert client.connect.call_args.kwargs["sock"] is None
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_pinned_ip_socket_connect_failure_maps_to_connection_error():
    """A raw-socket failure (the pinned IP is unreachable) must surface the
    same ConnectionError as a paramiko-level connect failure, not an
    unhandled OSError."""
    from app.ssh_manager import ConnectionError as SSHConnectionError

    manager = SSHSessionManager(cleanup_interval=3600)
    try:
        client = _mock_client()
        with (
            patch("app.ssh_manager.paramiko.SSHClient", return_value=client),
            patch(
                "app.ssh_manager.socket.create_connection",
                side_effect=OSError("connection refused"),
            ),
        ):
            with pytest.raises(SSHConnectionError):
                await manager.create_session(
                    host="dead-host",
                    port=22,
                    username="root",
                    password="x",
                    pinned_ip="203.0.113.5",
                )
            client.connect.assert_not_called()
    finally:
        await manager.close_all()
