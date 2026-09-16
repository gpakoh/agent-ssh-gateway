"""Tests for GatewayClient job cancellation surface."""

from __future__ import annotations

from examples.mcp_server.gateway_client import GatewayClient, GatewayClientError


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


def test_gateway_client_resolve_submission_uses_exact_key(monkeypatch):
    calls = []

    def fake_get(self, path, params=None, timeout=30):
        calls.append((path, params, timeout))
        return {"job_id": "job-historical"}

    monkeypatch.setattr(GatewayClient, "_get", fake_get)
    client = GatewayClient(base_url="https://gateway.example.test", api_key="key")

    assert client.resolve_submission_job("task:project-key:task-1") == "job-historical"
    assert calls == [
        (
            "/api/jobs/submissions/resolve",
            {"submission_key": "task:project-key:task-1"},
            30,
        )
    ]


def test_gateway_client_resolve_submission_family_uses_explicit_family_mode(monkeypatch):
    calls = []

    def fake_get(self, path, params=None, timeout=30):
        calls.append((path, params, timeout))
        return {"job_id": "job-family"}

    monkeypatch.setattr(GatewayClient, "_get", fake_get)
    client = GatewayClient(base_url="https://gateway.example.test", api_key="key")

    assert (
        client.resolve_submission_job_family("task:project-key:task-1:attempt:")
        == "job-family"
    )
    assert calls == [
        (
            "/api/jobs/submissions/resolve",
            {
                "submission_key": "task:project-key:task-1:attempt:",
                "family": "true",
            },
            30,
        )
    ]


def test_gateway_client_resolve_submission_family_treats_typed_404_as_missing(monkeypatch):
    def fake_get(self, path, params=None, timeout=30):
        raise GatewayClientError(
            "missing",
            status_code=404,
            body={"detail": {"code": "SUBMISSION_NOT_FOUND"}},
        )

    monkeypatch.setattr(GatewayClient, "_get", fake_get)
    client = GatewayClient(base_url="https://gateway.example.test", api_key="key")

    assert client.resolve_submission_job_family("task:project-key:missing:attempt:") is None


def test_gateway_client_resolve_submission_treats_only_typed_404_as_missing(monkeypatch):
    def fake_get(self, path, params=None, timeout=30):
        raise GatewayClientError(
            "missing",
            status_code=404,
            body={"detail": {"code": "SUBMISSION_NOT_FOUND"}},
        )

    monkeypatch.setattr(GatewayClient, "_get", fake_get)
    client = GatewayClient(base_url="https://gateway.example.test", api_key="key")

    assert client.resolve_submission_job("task:project-key:missing") is None


def test_gateway_client_resolve_submission_does_not_hide_untyped_404(monkeypatch):
    def fake_get(self, path, params=None, timeout=30):
        raise GatewayClientError("route missing", status_code=404, body={})

    monkeypatch.setattr(GatewayClient, "_get", fake_get)
    client = GatewayClient(base_url="https://gateway.example.test", api_key="key")

    try:
        client.resolve_submission_job("task:project-key:task-1")
    except GatewayClientError as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("untyped 404 must remain a hard error")
