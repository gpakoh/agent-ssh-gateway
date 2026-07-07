"""SSH session management using Paramiko."""

import asyncio
import builtins
import io
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

import paramiko
from paramiko.ssh_exception import (
    AuthenticationException,
    NoValidConnectionsError,
    SSHException,
)

from app.config import settings
from app.known_hosts import HostKeyStore, KnownHostsPolicy, NullHostKeyStore
from app.security import SecretManager

# Lazy Import To Avoid Circular Dependency


class CommandResult(TypedDict):
    stdout: str
    stderr: str
    exit_code: int
    duration: float


_emit_event_fn: Callable[..., Any] | None = None


def _emit(event: str, **kw: Any) -> None:
    global _emit_event_fn
    fn = _emit_event_fn
    if fn is None:
        from app.event_hook_emitter import emit_event as _emit_event_fn

        fn = _emit_event_fn
    assert fn is not None
    task = asyncio.ensure_future(fn(event, **kw))

    def _on_emit_done(t: asyncio.Task) -> None:
        exc = t.exception()
        if exc:
            logger.error("_emit failed: %s", exc)

    task.add_done_callback(_on_emit_done)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------


class SSHManagerError(Exception):
    """Base exception for SSH manager errors."""

    pass


class ConnectionError(SSHManagerError):
    """Failed to establish SSH connection."""

    pass


class AuthenticationError(SSHManagerError):
    """SSH authentication failed."""

    pass


class SessionNotFoundError(SSHManagerError):
    """Session ID not found."""

    pass


class TimeoutError(SSHManagerError):
    """Command execution timed out."""

    pass


class ExecutionError(SSHManagerError):
    """Error during command execution."""

    pass


# ---------------------------------------------------------------------------
# Session Record
# ---------------------------------------------------------------------------


@dataclass
class SessionRecord:
    """Stores an active SSH session and its metadata.

    Credentials are used only for the initial connection and are not stored.
    Reconnect requires credentials to be provided again by the caller.
    """

    session_id: str
    client: paramiko.SSHClient
    host: str
    port: int
    username: str
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    reconnect_count: int = 0
    last_reconnect_reason: str | None = None
    owner_type: str = "master"
    owner_name: str | None = None
    owner_token_fingerprint: str | None = None

    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = time.time()

    @property
    def idle_time(self) -> float:
        """Seconds since last activity."""
        return time.time() - self.last_activity

    def is_connected(self) -> bool:
        """Check if the SSH connection is still active."""
        try:
            transport = self.client.get_transport()
            return transport is not None and transport.is_active()
        except Exception:
            return False


# ---------------------------------------------------------------------------
# SSH Session Manager
# ---------------------------------------------------------------------------


class SSHSessionManager:
    """Manages multiple SSH sessions with automatic cleanup."""

    def __init__(
        self,
        session_timeout: int = 300,
        cleanup_interval: int = 60,
        host_key_store: HostKeyStore | None = None,
    ) -> None:
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = asyncio.Lock()
        self._session_timeout = session_timeout
        self._cleanup_interval = cleanup_interval
        self._cleanup_task: asyncio.Task | None = None
        self._strict_host_key = settings.ssh_strict_host_key_checking
        self._host_key_store = host_key_store or NullHostKeyStore()
        try:
            self._secret_manager = (
                SecretManager(settings.encryption_key) if settings.encryption_key else None
            )
        except Exception:
            self._secret_manager = None
        if self._secret_manager is None:
            logger.warning(
                "No Encryption Key Configured — SSH Credentials Stored In Plaintext In Memory"
            )
            self._secret_manager = None

    async def start_cleanup_task(self) -> None:
        """Start the background cleanup coroutine."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Session Cleanup Task Started")

    async def stop_cleanup_task(self) -> None:
        """Stop the background cleanup coroutine."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("Session Cleanup Task Stopped")

    async def _cleanup_loop(self) -> None:
        """Periodically remove stale sessions."""
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval)
                await self.cleanup_stale_sessions()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Cleanup loop error: %s", exc)

    async def cleanup_stale_sessions(self) -> int:
        """Close sessions that have been idle longer than timeout. Returns count of closed."""
        stale: list[SessionRecord] = []
        now = time.time()

        async with self._lock:
            for sid, record in list(self._sessions.items()):
                if now - record.last_activity > self._session_timeout:
                    del self._sessions[sid]
                    stale.append(record)

        for record in stale:
            logger.info(
                "Closing stale session %s (idle %.0fs)", record.session_id, record.idle_time
            )
            try:
                record.client.close()
            except Exception as exc:
                logger.warning("Error closing stale session %s: %s", record.session_id, exc)

        return len(stale)

    async def close_all(self) -> None:
        """Close all active sessions."""
        async with self._lock:
            ids = list(self._sessions.keys())
        for sid in ids:
            try:
                await self.disconnect(sid)
            except Exception as exc:
                logger.error("Error closing session %s: %s", sid, exc)

    def _get_host_key_policy(self, port: int = 22):
        """Return host key policy based on configuration.

        When a host key store is configured, use KnownHostsPolicy
        (checks store, rejects unknown/changed). When no store is
        configured, use RejectPolicy if strict, AutoAddPolicy otherwise.
        """
        if not isinstance(self._host_key_store, NullHostKeyStore):
            return KnownHostsPolicy(self._host_key_store, port=port)
        if self._strict_host_key:
            return paramiko.RejectPolicy()
        return paramiko.AutoAddPolicy()

    def _encrypt_cred(self, value: str | None) -> str | None:
        if value is None or self._secret_manager is None:
            return value
        return self._secret_manager.encrypt(value)

    def _decrypt_cred(self, value: str | None) -> str | None:
        if value is None or self._secret_manager is None:
            return value
        return self._secret_manager.decrypt(value)

    # ------------------------------------------------------------------
    # Create Session
    # ------------------------------------------------------------------

    async def create_session(
        self,
        host: str,
        port: int,
        username: str,
        password: str | None = None,
        private_key: str | None = None,
        key_passphrase: str | None = None,
        owner_type: str = "master",
        owner_name: str | None = None,
        owner_token_fingerprint: str | None = None,
    ) -> str:
        """Create a new SSH session and return its session ID."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(self._get_host_key_policy(port=port))

        pkey = None
        if private_key:
            pkey = await self._load_private_key(private_key, key_passphrase)

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: client.connect(
                    hostname=host,
                    port=port,
                    username=username,
                    password=password,
                    pkey=pkey,
                    timeout=30,
                    banner_timeout=30,
                    auth_timeout=30,
                    look_for_keys=False,
                ),
            )
        except AuthenticationException as exc:
            client.close()
            raise AuthenticationError(
                f"Authentication failed for {username}@{host}: {exc}"
            ) from exc
        except (NoValidConnectionsError, SSHException, OSError) as exc:
            client.close()
            raise ConnectionError(f"Could not connect to {host}:{port}: {exc}") from exc

        # Увеличить размер окна ssh для потоковой передачи
        transport = client.get_transport()
        if transport:
            transport.window_size = 2**20
            transport.packetizer.REKEY_BYTES = 2**30
            transport.packetizer.REKEY_PACKETS = 2**30

        session_id = str(uuid.uuid4())
        record = SessionRecord(
            session_id=session_id,
            client=client,
            host=host,
            port=port,
            username=username,
            owner_type=owner_type,
            owner_name=owner_name,
            owner_token_fingerprint=owner_token_fingerprint,
        )

        async with self._lock:
            self._sessions[session_id] = record

        logger.info("SSH session %s created for %s@%s:%d", session_id, username, host, port)
        _emit(
            "session.connected",
            session_id=session_id,
            host=host,
            port=port,
            username=username,
        )
        return session_id

    async def reconnect(self, session_id: str) -> bool:
        """Reconnect a disconnected session.

        Credentials are not stored on SessionRecord, so reconnect requires
        fresh credentials via create_session().  In-memory reconnect is
        no longer supported as part of credential hygiene.

        Returns True if the session is already connected.
        """
        async with self._lock:
            record = self._sessions.get(session_id)
        if not record:
            raise SessionNotFoundError(f"Session {session_id} not found")

        if record.is_connected():
            return True

        logger.warning(
            "Session %s (%s@%s:%d) is disconnected. Credentials are not "
            "stored — call create_session() again with fresh credentials.",
            session_id,
            record.username,
            record.host,
            record.port,
        )
        return False

    async def _load_private_key(
        self, key_data: str, passphrase: str | None = None
    ) -> paramiko.PKey:
        """Load a private key from string data (never touches disk)."""
        key_file = io.StringIO(key_data)
        errors: list[str] = []

        for key_class in (
            paramiko.Ed25519Key,
            paramiko.RSAKey,
            paramiko.ECDSAKey,
            paramiko.DSSKey,
        ):
            key_file.seek(0)
            try:
                return key_class.from_private_key(key_file, password=passphrase or None)
            except SSHException as exc:
                errors.append(f"{key_class.__name__}: {exc}")
                continue

        raise AuthenticationError(f"Could not parse private key. Tried: {'; '.join(errors)}")

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute(self, session_id: str, command: str, timeout: int = 30) -> CommandResult:
        """Execute a command and return stdout, stderr, exit_code, duration.

        Auto-reconnects if the SSH connection is broken.
        """
        async with self._lock:
            record = self._sessions.get(session_id)
        if not record:
            raise SessionNotFoundError(f"Session {session_id} not found")

        # Auto-reconnect If Needed
        if not record.is_connected():
            logger.warning("Session %s disconnected, attempting auto-reconnect", session_id)
            reconnected = await self.reconnect(session_id)
            if not reconnected:
                raise ConnectionError(
                    f"Session {session_id} is disconnected and reconnection failed"
                )

        record.touch()
        host, port, username = record.host, record.port, record.username
        client = record.client
        loop = asyncio.get_event_loop()
        start = time.time()

        _emit(
            "command.started",
            session_id=session_id,
            host=host,
            port=port,
            username=username,
            command=command,
        )

        try:
            stdin, stdout, stderr = await loop.run_in_executor(
                None,
                lambda: client.exec_command(command, timeout=timeout),
            )
            # Close Stdin Immediately Since We Don't Need It
            stdin.channel.shutdown_write()

            # Read Stdout And Stderr With Timeout
            out_data = await asyncio.wait_for(
                loop.run_in_executor(None, stdout.read),
                timeout=timeout,
            )
            err_data = await asyncio.wait_for(
                loop.run_in_executor(None, stderr.read),
                timeout=timeout,
            )
            exit_code = stdout.channel.recv_exit_status()
        except builtins.TimeoutError:
            raise TimeoutError(f"Command timed out after {timeout}s: {command}") from None
        except SSHException as exc:
            # Try To Reconnect On SSH Errors
            logger.warning("SSH error during execution for session %s: %s", session_id, exc)
            raise ExecutionError(f"SSH error during execution: {exc}") from exc
        except Exception as exc:
            raise ExecutionError(f"Execution error: {exc}") from exc

        duration = time.time() - start
        record.touch()

        out_text = out_data.decode("utf-8", errors="replace")
        err_text = err_data.decode("utf-8", errors="replace")

        _emit(
            "command.completed" if exit_code == 0 else "command.failed",
            session_id=session_id,
            host=host,
            port=port,
            username=username,
            command=command,
            exit_code=exit_code,
            duration=duration,
            stdout=out_text,
            stderr=err_text,
        )

        return {
            "stdout": out_text,
            "stderr": err_text,
            "exit_code": exit_code,
            "duration": round(duration, 3),
        }

    # ------------------------------------------------------------------
    # Streaming Execute (websocket)
    # ------------------------------------------------------------------

    async def execute_stream(
        self,
        session_id: str,
        command: str,
        timeout: int = 600,
        cancel_event: asyncio.Event | None = None,
    ):
        """Execute a command and yield (type, data) tuples for WebSocket streaming.

        If cancel_event is provided and set, closes the channel and stops streaming.
        """
        async with self._lock:
            record = self._sessions.get(session_id)
        if not record:
            raise SessionNotFoundError(f"Session {session_id} not found")

        record.touch()
        host, port, username = record.host, record.port, record.username
        client = record.client
        loop = asyncio.get_event_loop()

        _emit(
            "command.started",
            session_id=session_id,
            host=host,
            port=port,
            username=username,
            command=command,
        )

        try:
            stdin, stdout, stderr = await loop.run_in_executor(
                None,
                lambda: client.exec_command(command),
            )
            stdin.channel.shutdown_write()

            out_channel = stdout.channel
            err_channel = stderr.channel
            deadline = time.monotonic() + timeout

            # Stream Output In Chunks
            while not out_channel.exit_status_ready():
                if cancel_event and cancel_event.is_set():
                    out_channel.close()
                    yield ("exit", "-1")
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Command execution timed out after {timeout}s")
                record.touch()

                if out_channel.recv_ready():
                    data = out_channel.recv(4096).decode("utf-8", errors="replace")
                    yield ("stdout", data)

                if err_channel.recv_stderr_ready():
                    data = err_channel.recv_stderr(4096).decode("utf-8", errors="replace")
                    yield ("stderr", data)

                await asyncio.sleep(0.05)

            # Drain Remaining Output
            while out_channel.recv_ready():
                data = out_channel.recv(4096).decode("utf-8", errors="replace")
                yield ("stdout", data)
            while err_channel.recv_stderr_ready():
                data = err_channel.recv_stderr(4096).decode("utf-8", errors="replace")
                yield ("stderr", data)

            exit_code = out_channel.recv_exit_status()
            yield ("exit", str(exit_code))

            _emit(
                "command.completed" if exit_code == 0 else "command.failed",
                session_id=session_id,
                host=host,
                port=port,
                username=username,
                command=command,
                exit_code=exit_code,
            )

        except SSHException as exc:
            _emit(
                "command.failed",
                session_id=session_id,
                host=host,
                port=port,
                username=username,
                command=command,
            )
            yield ("error", str(exc))
        except Exception as exc:
            _emit(
                "command.failed",
                session_id=session_id,
                host=host,
                port=port,
                username=username,
                command=command,
            )
            yield ("error", str(exc))
        finally:
            record.touch()

    # ------------------------------------------------------------------
    # PTY Channel
    # ------------------------------------------------------------------

    async def create_pty_channel(self, session_id: str, term: str, rows: int, cols: int):
        async with self._lock:
            record = self._sessions.get(session_id)
        if not record:
            raise SessionNotFoundError(f"Session {session_id} not found")
        if not record.is_connected():
            raise ConnectionError(f"Session {session_id} is not connected")
        transport = record.client.get_transport()
        if transport is None:
            raise ConnectionError(f"Session {session_id} has no transport")
        channel = transport.open_session()
        channel.get_pty(term=term, width=cols, height=rows)
        channel.invoke_shell()
        record.touch()
        return channel

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    async def disconnect(self, session_id: str) -> None:
        """Close an SSH session."""
        async with self._lock:
            record = self._sessions.pop(session_id, None)

        if not record:
            raise SessionNotFoundError(f"Session {session_id} not found")

        host, port, username = record.host, record.port, record.username

        try:
            record.client.close()
        except Exception as exc:
            logger.warning("Error closing client for session %s: %s", session_id, exc)

        logger.info("SSH session %s disconnected", session_id)
        _emit(
            "session.disconnected",
            session_id=session_id,
            host=host,
            port=port,
            username=username,
            reason="manual",
        )

    # ------------------------------------------------------------------
    # List Sessions
    # ------------------------------------------------------------------

    async def get_session(self, session_id: str) -> SessionRecord | None:
        """Get a session by ID."""
        async with self._lock:
            return self._sessions.get(session_id)

    async def list_sessions(self) -> list[SessionRecord]:
        """Return list of active session records."""
        async with self._lock:
            return list(self._sessions.values())
