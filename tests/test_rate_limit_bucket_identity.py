"""Opaque rate-limit bucket diagnostics: safe per-source correlation, no raw IP leak."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from fastapi import Request

from app.main import rate_limit_exceeded_handler
from app.security import rate_limit_bucket_id
from examples.mcp_server.gateway_client import GatewayClientError
from examples.mcp_server.mcp_infra.gateway_errors import _gateway_error_details

_BUCKET_RE = re.compile(r"^bkt_[0-9a-f]{24}$")


def _request(
    source_ip: str = "198.51.100.10",
    *,
    path: str = "/api/ssh/execute",
    route: str = "/api/ssh/execute",
    method: str = "POST",
) -> Request:
    scope = {
        "type": "http",
        "scheme": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "client": (source_ip, 43000),
        "server": ("test.local", 80),
        "root_path": "",
        "route": SimpleNamespace(path=route),
    }
    return Request(scope)


def test_bucket_identity_repeats_only_for_same_source_route_and_lane():
    first = _request()
    same = _request()
    other_source = _request("198.51.100.11")
    other_route = _request(path="/api/ssh/connect", route="/api/ssh/connect")
    other_method = _request(method="GET")
    identity = rate_limit_bucket_id(first, bucket_class="ip")
    assert _BUCKET_RE.fullmatch(identity)
    assert rate_limit_bucket_id(same, bucket_class="ip") == identity
    assert rate_limit_bucket_id(other_source, bucket_class="ip") != identity
    assert rate_limit_bucket_id(other_route, bucket_class="ip") != identity
    assert rate_limit_bucket_id(other_method, bucket_class="ip") != identity
    assert rate_limit_bucket_id(first, bucket_class="master") != identity
    assert "198.51.100" not in identity
    assert "execute" not in identity


def test_route_template_reuses_bucket_diagnostic_for_dynamic_path_ids():
    req_a = _request(path="/api/jobs/A/status", route="/api/jobs/{job_id}/status", method="GET")
    req_b = _request(path="/api/jobs/B/status", route="/api/jobs/{job_id}/status", method="GET")
    assert rate_limit_bucket_id(req_a, bucket_class="ip") == rate_limit_bucket_id(req_b, bucket_class="ip")


@pytest.mark.asyncio
async def test_http_429_exposes_only_opaque_identity_and_existing_backoff():
    request = _request()
    limit = SimpleNamespace(limit=SimpleNamespace(GRANULARITY=45))
    exceeded = SimpleNamespace(limit=limit, detail="3 per 45 seconds")
    result = await rate_limit_exceeded_handler(request, exceeded)
    body = json.loads(result.body)
    assert result.status_code == 429
    assert body["code"] == "RATE_LIMIT_EXCEEDED"
    assert body["details"]["retry_after_seconds"] == 45
    assert body["details"]["bucket_class"] == "ip"
    bucket_id = body["details"]["bucket_id"]
    assert _BUCKET_RE.fullmatch(bucket_id)
    assert result.headers["X-RateLimit-Bucket-ID"] == bucket_id
    assert result.headers["X-RateLimit-Bucket"] == "ip"
    assert result.headers["Retry-After"] == "45"
    assert "198.51.100" not in str(body)
    assert "198.51.100" not in str(result.headers)


@pytest.mark.asyncio
async def test_without_request_avoids_inventing_bucket_identity():
    """Synthetic/diagnostic 429s have no proven client/IP bucket."""
    exceeded = SimpleNamespace(
        limit=SimpleNamespace(limit=SimpleNamespace(GRANULARITY=30)),
        detail="3 per 30 seconds",
    )
    response = await rate_limit_exceeded_handler(None, exceeded)
    body = json.loads(response.body)
    assert response.status_code == 429
    assert body["code"] == "RATE_LIMIT_EXCEEDED"
    assert body["details"]["bucket_class"] == "unknown"
    assert body["details"]["retry_after_seconds"] == 30
    assert "bucket_id" not in body["details"]
    assert "X-RateLimit-Bucket-ID" not in response.headers


@pytest.mark.parametrize("structure", ["flat", "nested"])
def test_mcp_propagates_valid_bucket_id_only(structure):
    bucket = rate_limit_bucket_id(_request(), bucket_class="ip")
    detail = {
        "retry_after_seconds": 15,
        "bucket_class": "ip",
        "bucket_id": bucket,
        "operation_class": "execute",
    }
    payload = {
        "code": "RATE_LIMIT_EXCEEDED",
        "message": "Rate limit exceeded",
        "retryable": True,
        "details": detail,
    }
    body = {"detail": payload} if structure == "nested" else payload
    exc = GatewayClientError("429", status_code=429, body=body)
    metadata = _gateway_error_details(exc)
    assert metadata is not None
    assert metadata["gateway_code"] == "RATE_LIMIT_EXCEEDED"
    assert metadata["retry_after_seconds"] == 15
    assert metadata["bucket_id"] == bucket


@pytest.mark.parametrize("untrusted", [
    "198.51.100.10",
    "bkt_001122",
    "bkt_" + "f" * 24 + "\\nsecret",
    "bkt_" + "G" * 24,
    "bearer secret token",
    ["bkt_" + "f" * 24],
    12345,
])
def test_mcp_drops_untrusted_bucket_id_but_keeps_typed_retry(untrusted):
    exc = GatewayClientError(
        "429",
        status_code=429,
        body={
            "code": "RATE_LIMIT_EXCEEDED",
            "details": {
                "bucket_id": untrusted,
                "bucket_class": "ip",
                "retry_after_seconds": 60,
            },
        },
    )
    metadata = _gateway_error_details(exc)
    assert metadata is not None
    assert "bucket_id" not in metadata
    assert metadata["retry_after_seconds"] == 60
    assert metadata["bucket_class"] == "ip"
