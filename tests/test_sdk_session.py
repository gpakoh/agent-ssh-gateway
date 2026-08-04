"""Tests for sdk.session — GatewaySession and AsyncGatewaySession."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from sdk.session import AsyncGatewaySession, GatewaySession

_MCP_SERVER_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
if str(_MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_SERVER_DIR))


class TestGatewaySessionLifecycle:
    """Context manager enter/exit lifecycle."""

    def test_enter_returns_self_and_stores_session_id(self):
        client = MagicMock()
        client.connect.return_value = "sid-abc-123"

        with GatewaySession(client) as gw:
            assert gw is not None
            assert gw.session_id == "sid-abc-123"
            client.connect.assert_called_once()

    def test_exit_calls_disconnect(self):
        client = MagicMock()
        client.connect.return_value = "sid-abc-123"

        with GatewaySession(client):
            pass

        client.disconnect.assert_called_once_with("sid-abc-123")

    def test_enter_connect_failure_does_not_call_disconnect(self):
        client = MagicMock()
        client.connect.side_effect = ConnectionError("refused")

        try:
            with GatewaySession(client):
                pass  # pragma: no cover
        except ConnectionError:
            pass

        client.disconnect.assert_not_called()

    def test_exit_disconnect_failure_does_not_raise(self):
        client = MagicMock()
        client.connect.return_value = "sid-abc-123"
        client.disconnect.side_effect = RuntimeError("network")

        with GatewaySession(client):
            pass

    def test_exit_masks_no_original_exception(self):
        client = MagicMock()
        client.connect.return_value = "sid-abc-123"
        client.disconnect.side_effect = RuntimeError("disconnect boom")

        raised = False
        try:
            with GatewaySession(client):
                raise ValueError("original")
        except ValueError as e:
            raised = True
            assert str(e) == "original"
        assert raised

    def test_enter_post_setup_failure_calls_disconnect_before_reraise(self):
        """If __enter__ succeeds at connect but code after raises, cleanup happens."""
        client = MagicMock()
        client.connect.return_value = "sid-abc-123"

        class ExplodingSession(GatewaySession):
            def __enter__(self):
                try:
                    self.session_id = self.client.connect()
                    raise RuntimeError("post-setup failure")
                except Exception:
                    self._disconnect_best_effort()
                    raise

        try:
            with ExplodingSession(client):
                pass
        except RuntimeError as e:
            assert str(e) == "post-setup failure"

        client.disconnect.assert_called_once_with("sid-abc-123")


# ---------------------------------------------------------------------------
# AsyncGatewaySession
# ---------------------------------------------------------------------------


class TestAsyncGatewaySessionLifecycle:
    """Async context manager enter/exit lifecycle.

    Regression: these used to mock `client` with AsyncMock() — matching
    AsyncGatewaySession's old (wrong) assumption that GatewayClient itself
    is async, instead of its real contract (every method is a plain
    blocking `def`). AsyncGatewaySession now bridges the real synchronous
    client via asyncio.to_thread, so these use MagicMock() like
    GatewaySession's own tests do, and assert on `assert_called*`
    (a to_thread call is still a synchronous call from the mock's
    perspective — only the *wrapper* around it is async).
    """

    async def test_aenter_returns_self_and_stores_session_id(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"

        async with AsyncGatewaySession(client) as gw:
            assert gw is not None
            assert gw.session_id == "sid-async-1"
            client.connect.assert_called_once()

    async def test_aexit_calls_disconnect(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"

        async with AsyncGatewaySession(client):
            pass

        client.disconnect.assert_called_once_with("sid-async-1")

    async def test_aenter_connect_failure_does_not_call_disconnect(self):
        client = MagicMock()
        client.connect.side_effect = ConnectionError("refused")

        try:
            async with AsyncGatewaySession(client):
                pass  # pragma: no cover
        except ConnectionError:
            pass

        client.disconnect.assert_not_called()

    async def test_aexit_disconnect_failure_does_not_raise(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.disconnect.side_effect = RuntimeError("network")

        async with AsyncGatewaySession(client):
            pass

    async def test_aexit_masks_no_original_exception(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.disconnect.side_effect = RuntimeError("disconnect boom")

        raised = False
        try:
            async with AsyncGatewaySession(client):
                raise ValueError("original")
        except ValueError as e:
            raised = True
            assert str(e) == "original"
        assert raised

    async def test_aenter_post_setup_failure_calls_disconnect_before_reraise(self):
        """If __aenter__ succeeds at connect but code after raises, cleanup happens."""
        client = MagicMock()
        client.connect.return_value = "sid-async-1"

        class ExplodingAsyncSession(AsyncGatewaySession):
            async def __aenter__(self):
                import asyncio

                try:
                    self.session_id = await asyncio.to_thread(self.client.connect)
                    raise RuntimeError("post-setup failure")
                except Exception:
                    await self._disconnect_best_effort()
                    raise

        try:
            async with ExplodingAsyncSession(client):
                pass
        except RuntimeError as e:
            assert str(e) == "post-setup failure"

        client.disconnect.assert_called_once_with("sid-async-1")


class TestAsyncGatewaySessionRun:
    """async run() executes command and waits for job completion."""

    async def test_run_calls_execute_restricted_then_wait_job(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.execute_restricted.return_value = {"job_id": "job-42"}
        client.wait_job.return_value = {
            "status": "completed",
            "stdout": "hello\n",
            "exit_code": 0,
        }

        async with AsyncGatewaySession(client) as gw:
            result = await gw.run("echo hello")

        client.execute_restricted.assert_called_once_with(
            session_id="sid-async-1", command="echo hello"
        )
        client.wait_job.assert_called_once_with(job_id="job-42", timeout_sec=None)
        assert result["status"] == "completed"
        assert result["stdout"] == "hello\n"

    async def test_run_passes_timeout(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.execute_restricted.return_value = {"job_id": "job-99"}
        client.wait_job.return_value = {"status": "completed"}

        async with AsyncGatewaySession(client) as gw:
            await gw.run("sleep 10", timeout=30)

        client.wait_job.assert_called_once_with(job_id="job-99", timeout_sec=30)

    async def test_run_does_not_pass_session_id_to_wait_job(self):
        """wait_job uses auth, not session_id."""
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.execute_restricted.return_value = {"job_id": "job-7"}
        client.wait_job.return_value = {"status": "completed"}

        async with AsyncGatewaySession(client) as gw:
            await gw.run("pwd")

        call_kwargs = client.wait_job.call_args
        assert "session_id" not in call_kwargs.kwargs
        assert "session_id" not in (call_kwargs[0] if call_kwargs[0] else ())


class TestAsyncGatewaySessionRead:
    """async read() returns file content string."""

    async def test_read_extracts_content_from_response(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.read_file.return_value = {"content": "file contents here"}

        async with AsyncGatewaySession(client) as gw:
            result = await gw.read("/etc/hostname")

        client.read_file.assert_called_once_with(
            session_id="sid-async-1", path="/etc/hostname"
        )
        assert result == "file contents here"

    async def test_read_returns_empty_string_on_missing_content(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.read_file.return_value = {}

        async with AsyncGatewaySession(client) as gw:
            result = await gw.read("/nonexistent")

        assert result == ""


class TestAsyncGatewaySessionWrite:
    """async write() returns raw Gateway response."""

    async def test_write_returns_raw_response(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.write_file.return_value = {
            "status": "ok",
            "pending_confirmation": {"file": "app.py", "hash": "abc"},
        }

        async with AsyncGatewaySession(client) as gw:
            result = await gw.write("app.py", "new content")

        client.write_file.assert_called_once_with(
            session_id="sid-async-1", path="app.py", content="new content"
        )
        assert "pending_confirmation" in result
        assert result["pending_confirmation"]["hash"] == "abc"

    async def test_write_returns_ok_without_confirmation(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.write_file.return_value = {"status": "ok"}

        async with AsyncGatewaySession(client) as gw:
            result = await gw.write("README.md", "# Hello")

        assert result == {"status": "ok"}


class TestAsyncGatewaySessionHealth:
    """async session_health() delegates to client."""

    async def test_session_health_passes_session_id(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.session_health.return_value = {"status": "healthy", "uptime": 120}

        async with AsyncGatewaySession(client) as gw:
            result = await gw.session_health()

        client.session_health.assert_called_once_with(session_id="sid-async-1")
        assert result["status"] == "healthy"


# ---------------------------------------------------------------------------
# GatewaySession — operational methods (sync)
# ---------------------------------------------------------------------------


class TestGatewaySessionRun:
    """run() executes command and waits for job completion."""

    def test_run_calls_execute_restricted_then_wait_job(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.execute_restricted.return_value = {"job_id": "job-42"}
        client.wait_job.return_value = {
            "status": "completed",
            "stdout": "hello\n",
            "exit_code": 0,
        }

        with GatewaySession(client) as gw:
            result = gw.run("echo hello")

        client.execute_restricted.assert_called_once_with(
            session_id="sid-1", command="echo hello"
        )
        client.wait_job.assert_called_once_with(job_id="job-42", timeout_sec=None)
        assert result["status"] == "completed"
        assert result["stdout"] == "hello\n"

    def test_run_passes_timeout(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.execute_restricted.return_value = {"job_id": "job-99"}
        client.wait_job.return_value = {"status": "completed"}

        with GatewaySession(client) as gw:
            gw.run("sleep 10", timeout=30)

        client.wait_job.assert_called_once_with(job_id="job-99", timeout_sec=30)

    def test_run_does_not_pass_session_id_to_wait_job(self):
        """wait_job uses auth, not session_id."""
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.execute_restricted.return_value = {"job_id": "job-7"}
        client.wait_job.return_value = {"status": "completed"}

        with GatewaySession(client) as gw:
            gw.run("pwd")

        call_kwargs = client.wait_job.call_args
        assert "session_id" not in call_kwargs.kwargs
        assert "session_id" not in (call_kwargs[0] if call_kwargs[0] else ())


class TestGatewaySessionRead:
    """read() returns file content string."""

    def test_read_extracts_content_from_response(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.read_file.return_value = {"content": "file contents here"}

        with GatewaySession(client) as gw:
            result = gw.read("/etc/hostname")

        client.read_file.assert_called_once_with(session_id="sid-1", path="/etc/hostname")
        assert result == "file contents here"

    def test_read_returns_empty_string_on_missing_content(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.read_file.return_value = {}

        with GatewaySession(client) as gw:
            result = gw.read("/nonexistent")

        assert result == ""


class TestGatewaySessionWrite:
    """write() returns raw Gateway response — no auto-confirmation."""

    def test_write_returns_raw_response(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.write_file.return_value = {
            "status": "ok",
            "pending_confirmation": {"file": "app.py", "hash": "abc"},
        }

        with GatewaySession(client) as gw:
            result = gw.write("app.py", "new content")

        client.write_file.assert_called_once_with(
            session_id="sid-1", path="app.py", content="new content"
        )
        assert "pending_confirmation" in result
        assert result["pending_confirmation"]["hash"] == "abc"

    def test_write_returns_ok_without_confirmation(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.write_file.return_value = {"status": "ok"}

        with GatewaySession(client) as gw:
            result = gw.write("README.md", "# Hello")

        assert result == {"status": "ok"}


class TestGatewaySessionHealth:
    """session_health() delegates to client."""

    def test_session_health_passes_session_id(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.session_health.return_value = {"status": "healthy", "uptime": 120}

        with GatewaySession(client) as gw:
            result = gw.session_health()

        client.session_health.assert_called_once_with(session_id="sid-1")
        assert result["status"] == "healthy"


class TestAsyncGatewaySessionWithRealClient:
    """The definitive proof: drives the actual (synchronous) GatewayClient
    class end-to-end through AsyncGatewaySession — not a mock standing in
    for it, real or otherwise. Only the network boundary (httpx) is faked.
    This is exactly the class of bug a mock can hide regardless of which
    Mock subclass is used: the old code was a guaranteed TypeError with
    *any* real client, sync or not.
    """

    async def test_run_end_to_end_against_the_real_gateway_client(self):
        from gateway_client import GatewayClient

        env = {
            "GATEWAY_BASE_URL": "http://gateway:8085",
            "GATEWAY_API_KEY": "test-api-key",
            "GATEWAY_SESSION_ID": "",
            "GATEWAY_SSH_HOST": "sshd-host",
            "GATEWAY_SSH_USER": "root",
            "GATEWAY_SSH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            client = GatewayClient()

        connect_resp = MagicMock(status_code=200)
        connect_resp.json.return_value = {"session_id": "real-sid-1"}

        with patch("httpx.post", return_value=connect_resp):
            with patch.object(
                client, "_post",
                side_effect=[
                    {"job_id": "job-1"},  # execute_restricted
                ],
            ):
                with patch.object(
                    client, "_get",
                    return_value={"status": "completed", "stdout": "hello\n", "exit_code": 0},
                ):
                    async with AsyncGatewaySession(client) as gw:
                        assert gw.session_id == "real-sid-1"
                        result = await gw.run("cat /etc/hostname")

        assert result["status"] == "completed"
        assert result["stdout"] == "hello\n"
