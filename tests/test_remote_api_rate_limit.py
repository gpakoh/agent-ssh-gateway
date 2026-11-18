"""P2-06: Gitea/GitHub upstream 429 must preserve safe backoff without replaying writes."""

from __future__ import annotations

import httpx
import pytest

from examples.mcp_server.mcp_infra.adapters.remote import _remote_api_error


def _status_error(
    status: int,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request(method, "https://git.invalid/api/v1/repos/example/repo")
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError(
        "unsafe upstream url and token=do-not-echo", request=request, response=response
    )


@pytest.mark.parametrize("source", ["gitea", "github"])
def test_upstream_rate_limit_read_exposes_bounded_retry(source: str) -> None:
    result = _remote_api_error(
        f"{source}_get_repo",
        source,
        _status_error(
            429,
            headers={
                "Retry-After": "7",
                "X-RateLimit-Bucket": "sensitive-upstream-bucket",
                "X-RateLimit-Bucket-ID": "sensitive-upstream-identity",
            },
        ),
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert result["error"]["retryable"] is True
    assert result["error"]["details"] == {
        "retry_after_seconds": 7,
        "bucket_class": "remote_api",
        "operation_class": "read",
    }
    assert "retry" in result["error"]["hint"].lower()
    assert result["meta"]["source"] == source
    assert "token=" not in str(result)
    assert "sensitive-upstream" not in str(result)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("0", 1),
        ("00003", 3),
        ("86400", 86400),
        ("86401", None),
        ("99999", None),
        ("100000", None),
        ("-2", None),
        ("+2", None),
        ("1.5", None),
        ("Wed, 18 Nov 2026 11:00:00 GMT", None),
        ("", None),
    ],
)
def test_retry_after_never_invents_or_trusts_unsafe_values(
    header: str, expected: int | None
) -> None:
    result = _remote_api_error(
        "gitea_list_commits",
        "gitea",
        _status_error(429, headers={"Retry-After": header}),
    )
    assert result["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    details = result["error"]["details"]
    assert details["bucket_class"] == "remote_api"
    assert details["operation_class"] == "read"
    assert details.get("retry_after_seconds") == expected


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "GET"])
def test_mutating_tool_never_advertises_automatic_retry(method: str) -> None:
    result = _remote_api_error(
        "gitea_merge_pull_request",
        "gitea",
        _status_error(429, method=method, headers={"Retry-After": "11"}),
    )
    assert result["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["retry_after_seconds"] == 11
    assert result["error"]["details"]["operation_class"] == "mutation"
    hint = result["error"]["hint"].lower()
    assert "read" in hint or "inspect" in hint
    assert "replay" in hint or "repeat" in hint


def test_read_tool_with_unexpected_write_method_fails_closed() -> None:
    result = _remote_api_error(
        "github_get_repo", "github", _status_error(429, method="POST")
    )
    assert result["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["operation_class"] == "mutation"


def test_unrelated_404_stays_nonretryable_remote_error() -> None:
    result = _remote_api_error("gitea_get_repo", "gitea", _status_error(404))
    assert result["error"]["code"] == "REMOTE_API_ERROR"
    assert result["error"]["retryable"] is False


def test_remote_auth_error_stays_auth_error() -> None:
    result = _remote_api_error("gitea_get_repo", "gitea", PermissionError("unauthorized"))
    assert result["error"]["code"] == "AUTH_ERROR"
    assert result["error"]["retryable"] is False
