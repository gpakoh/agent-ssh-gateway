"""Tests for circuit breaker wiring into SSHSessionManager.create_session().

Verifies the breaker actually protects the connection path (not just the
standalone state machine tested in test_circuit_breaker.py): connection
failures open the breaker and further attempts are rejected without hitting
the network again; authentication failures do not open the breaker.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from paramiko.ssh_exception import AuthenticationException, NoValidConnectionsError

from app.circuit_breaker import CircuitBreakerRegistry, CircuitState
from app.ssh_manager import AuthenticationError, SSHSessionManager
from app.ssh_manager import ConnectionError as SSHConnectionError


def _mock_client(connect_side_effect=None):
    client = MagicMock()
    if connect_side_effect is not None:
        client.connect.side_effect = connect_side_effect
    client.get_transport.return_value = None
    return client


@pytest.mark.asyncio
async def test_connection_failure_opens_breaker_and_blocks_further_attempts():
    registry = CircuitBreakerRegistry()
    manager = SSHSessionManager(cleanup_interval=3600, circuit_breakers=registry)
    try:
        client = _mock_client(connect_side_effect=NoValidConnectionsError({("1.2.3.4", 22): "refused"}))
        with patch("app.ssh_manager.paramiko.SSHClient", return_value=client):
            breaker = await registry.get_breaker("dead-host", failure_threshold=2, recovery_timeout=3600)

            for _ in range(2):
                with pytest.raises(SSHConnectionError):
                    await manager.create_session(
                        host="dead-host", port=22, username="root", password="x"
                    )

            assert breaker.state == CircuitState.OPEN
            assert client.connect.call_count == 2

            # Breaker is open — a further call must be rejected WITHOUT
            # attempting another network connection.
            with pytest.raises(SSHConnectionError, match="Circuit breaker open"):
                await manager.create_session(
                    host="dead-host", port=22, username="root", password="x"
                )
            assert client.connect.call_count == 2  # unchanged — no new attempt
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_authentication_failure_does_not_open_breaker():
    registry = CircuitBreakerRegistry()
    manager = SSHSessionManager(cleanup_interval=3600, circuit_breakers=registry)
    try:
        client = _mock_client(connect_side_effect=AuthenticationException("bad creds"))
        with patch("app.ssh_manager.paramiko.SSHClient", return_value=client):
            for _ in range(5):
                with pytest.raises(AuthenticationError):
                    await manager.create_session(
                        host="live-host", port=22, username="root", password="wrong"
                    )

            breaker = await registry.get_breaker("live-host")
            assert breaker.state == CircuitState.CLOSED
            assert client.connect.call_count == 5  # every attempt reached the network
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_successful_connection_records_success():
    registry = CircuitBreakerRegistry()
    manager = SSHSessionManager(cleanup_interval=3600, circuit_breakers=registry)
    try:
        client = _mock_client()
        with patch("app.ssh_manager.paramiko.SSHClient", return_value=client):
            session_id = await manager.create_session(
                host="good-host", port=22, username="root", password="x"
            )
            assert session_id

            breaker = await registry.get_breaker("good-host")
            stats = await breaker.get_stats()
            assert stats["state"] == "closed"
            assert stats["failure_count"] == 0
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_no_registry_means_no_breaker_check():
    """Without a circuit_breakers registry, create_session behaves as before (no gating)."""
    manager = SSHSessionManager(cleanup_interval=3600)  # circuit_breakers=None
    try:
        client = _mock_client()
        with patch("app.ssh_manager.paramiko.SSHClient", return_value=client):
            session_id = await manager.create_session(
                host="good-host", port=22, username="root", password="x"
            )
            assert session_id
    finally:
        await manager.close_all()
