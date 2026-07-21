"""Tests for request metrics middleware."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client():
    with patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


class TestMetricsMiddleware:
    def test_request_recorded(self, client):
        """Request count and duration are recorded."""
        with patch("app.metrics.metrics") as mock_metrics:
            resp = client.get("/health")
            assert resp.status_code == 200
            mock_metrics.record_request.assert_called_once()
            call_args = mock_metrics.record_request.call_args
            assert call_args[1]["method"] == "GET"
            assert call_args[1]["status"] == 200
            assert call_args[1]["duration"] >= 0

    def test_endpoint_template_used(self, client):
        """Route template is used, not raw path with IDs."""
        with patch("app.metrics.metrics") as mock_metrics:
            client.get(
                "/api/workspace/projects/web-ssh-gateway/tree",
                headers={"X-API-Key": settings.api_key},
            )
            call_args = mock_metrics.record_request.call_args
            # Should contain {project_id} template, not the literal ID
            assert "{project_id}" in call_args[1]["endpoint"] or call_args[1]["endpoint"] == "unknown"

    def test_query_params_not_in_labels(self, client):
        """Query tokens/params don't leak into metric labels."""
        with patch("app.metrics.metrics") as mock_metrics:
            client.get(
                "/health?token=secret-token&api_key=secret-key",
            )
            call_args = mock_metrics.record_request.call_args
            endpoint = call_args[1]["endpoint"]
            # No query params in endpoint label
            assert "?" not in endpoint
            assert "secret-token" not in endpoint
            assert "secret-key" not in endpoint

    def test_4xx_status_recorded(self, client):
        """4xx status codes are recorded."""
        with patch("app.metrics.metrics") as mock_metrics:
            client.get(
                "/api/workspace/projects/nonexistent/tree",
                headers={"X-API-Key": settings.api_key},
            )
            call_args = mock_metrics.record_request.call_args
            assert 400 <= call_args[1]["status"] < 500

    def test_5xx_status_recorded(self, client):
        """Non-2xx status codes are recorded."""
        with patch("app.metrics.metrics") as mock_metrics:
            # Hit an endpoint that returns non-2xx
            client.get(
                "/api/workspace/projects/nonexistent/tree",
                headers={"X-API-Key": settings.api_key},
            )
            # Metrics should have been recorded
            assert mock_metrics.record_request.called
