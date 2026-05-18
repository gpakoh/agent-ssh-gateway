"""SSH session management using Paramiko."""

import asyncio
import io
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import paramiko
from paramiko.ssh_exception import (
    AuthenticationException,
    BadHostKeyException,
    SSHException,
    NoValidConnectionsError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
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
# Session record
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    """Stores an active SSH session and its metadata."""

    session_id: str
    client: paramiko.SSHClient
    host: str
    port: int
    username: str
    password: Optional[str] = None
    private_key: Optional[str] = None
    key_passphrase: Optional[str] = None
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    reconnect_count: int = 0
    last_reconnect_reason: Optional[str] = None

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

    def __init__(self, session_timeout: int = 300, cleanup_interval: int = 60) -> None:
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = asyncio.Lock()
        self._session_timeout = session_timeout
        self._cleanup_interval = cleanup_interval
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start_cleanup_task(self) -> None:
        """Start the background cleanup coroutine."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Session cleanup task started")

    async def stop_cleanup_task(self) -> None:
        """Stop the background cleanup coroutine."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("Session cleanup task stopped")

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
        stale_ids: list[str] = []
        now = time.time()

        async with self._lock:
            for sid, record in list(self._sessions.items()):
                if now - record.last_activity > self._session_timeout:
                    stale_ids.append(sid)

        for sid in stale_ids:
            logger.info("Closing stale session %s (idle %.0fs)", sid, self._sessions.get(sid, SessionRecord("", None, "", 0, "")).idle_time)
            await self.disconnect(sid)

        return len(stale_ids)

    async def close_all(self) -> None:
        """Close all active sessions."""
        async with self._lock:
            ids = list(self._sessions.keys())
        for sid in ids:
            try:
                await self.disconnect(sid)
            except Exception as exc:
                logger.error("Error closing session %s: %s", sid, exc)

    # ------------------------------------------------------------------
    # Create session
    # ------------------------------------------------------------------

    async def create_session(
        self,
        host: str,
        port: int,
        username: str,
        password: Optional[str] = None,
        private_key: Optional[str] = None,
        key_passphrase: Optional[str] = None,
    ) -> str:
        """Create a new SSH session and return its session ID."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

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
            raise AuthenticationError(f"Authentication failed for {username}@{host}: {exc}")
        except (NoValidConnectionsError, SSHException, OSError) as exc:
            client.close()
            raise ConnectionError(f"Could not connect to {host}:{port}: {exc}")

        # Увеличить размер окна SSH для больших файлов (50KB+)
        transport = client.get_transport()
        if transport:
            transport.window_size = 2**32
            transport.packetizer.REKEY_BYTES = 2**40
            transport.packetizer.REKEY_PACKETS = 2**40

        session_id = str(uuid.uuid4())
        record = SessionRecord(
            session_id=session_id,
            client=client,
            host=host,
            port=port,
            username=username,
            password=password,
            private_key=private_key,
            key_passphrase=key_passphrase,
        )

        async with self._lock:
            self._sessions[session_id] = record

        logger.info("SSH session %s created for %s@%s:%d", session_id, username, host, port)
        return session_id

    async def reconnect(self, session_id: str) -> bool:
        """Reconnect a disconnected session using stored credentials.
        
        Returns True if reconnection was successful.
        """
        async with self._lock:
            record = self._sessions.get(session_id)
        if not record:
            raise SessionNotFoundError(f"Session {session_id} not found")
        
        if record.is_connected():
            return True
        
        logger.info("Attempting to reconnect session %s (%s@%s:%d)", 
                   session_id, record.username, record.host, record.port)
        
        # Close old client if exists
        try:
            record.client.close()
        except Exception:
            pass
        
        # Create new client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        pkey = None
        if record.private_key:
            pkey = await self._load_private_key(record.private_key, record.key_passphrase)
        
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: client.connect(
                    hostname=record.host,
                    port=record.port,
                    username=record.username,
                    password=record.password,
                    pkey=pkey,
                    timeout=30,
                    banner_timeout=30,
                    auth_timeout=30,
                    look_for_keys=False,
                ),
            )
            
            # Configure window size for large files
            transport = client.get_transport()
            if transport:
                transport.window_size = 2**32
                transport.packetizer.REKEY_BYTES = 2**40
                transport.packetizer.REKEY_PACKETS = 2**40
            
            # Update record with new client
            record.client = client
            record.reconnect_count += 1
            record.last_reconnect_reason = "timeout"  # или можно определить точнее
            record.touch()
            
            logger.info("Session %s reconnected successfully (reconnect #%d)", 
                       session_id, record.reconnect_count)
            return True
            
        except AuthenticationException as exc:
            client.close()
            logger.error("Reconnection failed for session %s: Authentication failed: %s", session_id, exc)
            return False
        except (NoValidConnectionsError, SSHException, OSError) as exc:
            client.close()
            logger.error("Reconnection failed for session %s: %s", session_id, exc)
            return False

    async def _load_private_key(
        self, key_data: str, passphrase: Optional[str] = None
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

    async def execute(
        self, session_id: str, command: str, timeout: int = 30
    ) -> dict[str, object]:
        """Execute a command and return stdout, stderr, exit_code, duration.
        
        Auto-reconnects if the SSH connection is broken.
        """
        async with self._lock:
            record = self._sessions.get(session_id)
        if not record:
            raise SessionNotFoundError(f"Session {session_id} not found")

        # Auto-reconnect if needed
        if not record.is_connected():
            logger.warning("Session %s disconnected, attempting auto-reconnect", session_id)
            reconnected = await self.reconnect(session_id)
            if not reconnected:
                raise ConnectionError(f"Session {session_id} is disconnected and reconnection failed")

        record.touch()
        client = record.client
        loop = asyncio.get_event_loop()
        start = time.time()

        try:
            stdin, stdout, stderr = await loop.run_in_executor(
                None,
                lambda: client.exec_command(command, timeout=timeout),
            )
            # Close stdin immediately since we don't need it
            stdin.channel.shutdown_write()

            # Read stdout and stderr with timeout
            out_data = await asyncio.wait_for(
                loop.run_in_executor(None, stdout.read),
                timeout=timeout,
            )
            err_data = await asyncio.wait_for(
                loop.run_in_executor(None, stderr.read),
                timeout=timeout,
            )
            exit_code = stdout.channel.recv_exit_status()
        except asyncio.TimeoutError:
            raise TimeoutError(f"Command timed out after {timeout}s: {command}")
        except SSHException as exc:
            # Try to reconnect on SSH errors
            logger.warning("SSH error during execution for session %s: %s", session_id, exc)
            raise ExecutionError(f"SSH error during execution: {exc}")
        except Exception as exc:
            raise ExecutionError(f"Execution error: {exc}")

        duration = time.time() - start
        record.touch()

        return {
            "stdout": out_data.decode("utf-8", errors="replace"),
            "stderr": err_data.decode("utf-8", errors="replace"),
            "exit_code": exit_code,
            "duration": round(duration, 3),
        }

    # ------------------------------------------------------------------
    # Streaming execute (WebSocket)
    # ------------------------------------------------------------------

    async def execute_stream(self, session_id: str, command: str):
        """Execute a command and yield (type, data) tuples for WebSocket streaming."""
        async with self._lock:
            record = self._sessions.get(session_id)
        if not record:
            raise SessionNotFoundError(f"Session {session_id} not found")

        record.touch()
        client = record.client
        loop = asyncio.get_event_loop()

        try:
            stdin, stdout, stderr = await loop.run_in_executor(
                None,
                lambda: client.exec_command(command),
            )
            stdin.channel.shutdown_write()

            out_channel = stdout.channel
            err_channel = stderr.channel

            # Stream output in chunks
            while not out_channel.exit_status_ready():
                record.touch()

                if out_channel.recv_ready():
                    data = out_channel.recv(4096).decode("utf-8", errors="replace")
                    yield ("stdout", data)

                if err_channel.recv_stderr_ready():
                    data = err_channel.recv_stderr(4096).decode("utf-8", errors="replace")
                    yield ("stderr", data)

                await asyncio.sleep(0.05)

            # Drain remaining output
            while out_channel.recv_ready():
                data = out_channel.recv(4096).decode("utf-8", errors="replace")
                yield ("stdout", data)
            while err_channel.recv_stderr_ready():
                data = err_channel.recv_stderr(4096).decode("utf-8", errors="replace")
                yield ("stderr", data)

            exit_code = out_channel.recv_exit_status()
            yield ("exit", str(exit_code))

        except SSHException as exc:
            yield ("error", str(exc))
        except Exception as exc:
            yield ("error", str(exc))
        finally:
            record.touch()

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    async def disconnect(self, session_id: str) -> None:
        """Close an SSH session."""
        async with self._lock:
            record = self._sessions.pop(session_id, None)

        if not record:
            raise SessionNotFoundError(f"Session {session_id} not found")

        try:
            record.client.close()
        except Exception as exc:
            logger.warning("Error closing client for session %s: %s", session_id, exc)

        logger.info("SSH session %s disconnected", session_id)

    # ------------------------------------------------------------------
    # List sessions
    # ------------------------------------------------------------------

    async def get_session(self, session_id: str) -> Optional[SessionRecord]:
        """Get a session by ID."""
        async with self._lock:
            return self._sessions.get(session_id)

    async def list_sessions(self) -> list[SessionRecord]:
        """Return list of active session records."""
        async with self._lock:
            return list(self._sessions.values())
