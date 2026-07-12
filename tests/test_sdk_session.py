"""Tests for sdk.session — GatewaySession and AsyncGatewaySession."""

from unittest.mock import MagicMock

from sdk.session import GatewaySession


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
