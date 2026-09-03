"""Tests for GatewayClient job cancellation surface."""

from __future__ import annotations

from examples.mcp_server.gateway_client import GatewayClient


def test_gateway_client_cancel_job_posts_cancel_endpoint(monkeypatch):
    calls = []

    def fake_post(self, path, payload, **kwargs):
        calls.append((path, payload, kwargs))
        return {"status": "cancelling", "job_id": "job-1"}

    monkeypatch.setattr(GatewayClient, "_post", fake_post)

    client = GatewayClient(base_url="https://gateway.example.test", api_key="key")
    result = client.cancel_job("job-1")

    assert result == {"status": "cancelling", "job_id": "job-1"}
    assert calls == [("/api/jobs/job-1/cancel", {}, {})]
