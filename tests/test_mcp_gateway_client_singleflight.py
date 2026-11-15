"""Bounded, session-local in-flight HTTP health-read coalescing contract."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx

MCP_SERVER_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
if str(MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_SERVER_DIR))

from gateway_client import GatewayClient, GatewayClientError  # noqa: E402


def _response(status: int, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        headers={"Retry-After": "7", "X-RateLimit-Bucket": "ip"}
        if status == 429
        else {},
        request=httpx.Request("GET", "https://gateway.invalid/health"),
    )


def _run_overlapping_health(client: GatewayClient, response: httpx.Response) -> tuple[list[object], int]:
    entered = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    calls = 0
    lock = threading.Lock()

    def fake_get(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal calls
        with lock:
            calls += 1
        entered.set()
        assert release.wait(5), "test transport was not released"
        return response

    def second_call() -> object:
        second_started.set()
        return client.health()

    with patch("gateway_client.httpx.get", side_effect=fake_get):
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(client.health)
            try:
                assert entered.wait(3)
                second = workers.submit(second_call)
                assert second_started.wait(3)
                # Give the follower a chance to join while the leader is blocked.
                time.sleep(0.08)
            finally:
                release.set()
            results: list[object] = []
            for job in (first, second):
                try:
                    results.append(job.result(timeout=3))
                except GatewayClientError as exc:
                    results.append(exc)
    return results, calls


def test_scoped_health_reads_share_only_inflight_work() -> None:
    client = GatewayClient(base_url="https://gateway.invalid", api_key="same-auth").fork_session()
    response = _response(200, {"ready": True})
    results, calls = _run_overlapping_health(client, response)
    assert results == [{"ready": True}, {"ready": True}]
    assert calls == 1

    # Completed responses are never cached; a new request hits the wire.
    with patch("gateway_client.httpx.get", return_value=_response(200, {"ready": False})) as get:
        assert client.health() == {"ready": False}
    get.assert_called_once()


def test_scoped_429_followers_get_same_typed_error_and_next_request_is_fresh() -> None:
    client = GatewayClient(base_url="https://gateway.invalid", api_key="same-auth").fork_session()
    results, calls = _run_overlapping_health(
        client,
        _response(429, {"code": "RATE_LIMIT_EXCEEDED", "retryable": True}),
    )
    assert calls == 1
    assert all(isinstance(result, GatewayClientError) for result in results)
    assert all(result.status_code == 429 for result in results)
    assert all(result.body["details"] == {"retry_after_seconds": 7, "bucket_class": "ip"} for result in results)
    with patch("gateway_client.httpx.get", return_value=_response(200, {"ready": True})) as get:
        assert client.health() == {"ready": True}
    get.assert_called_once()


def test_auth_isolated_clients_never_coalesce_across_lifecycles() -> None:
    first = GatewayClient(base_url="https://gateway.invalid", api_key="auth-a").fork_session()
    second = GatewayClient(base_url="https://gateway.invalid", api_key="auth-b").fork_session()
    with patch("gateway_client.httpx.get", return_value=_response(200, {"ready": True})) as get:
        assert first.health() == {"ready": True}
        assert second.health() == {"ready": True}
    assert get.call_count == 2


def test_non_scoped_health_and_post_are_never_coalesced() -> None:
    original = GatewayClient(base_url="https://gateway.invalid", api_key="auth-a")
    scoped = original.fork_session()
    with patch("gateway_client.httpx.get", return_value=_response(200, {"ready": True})) as get:
        assert original.health() == {"ready": True}
        assert original.health() == {"ready": True}
    assert get.call_count == 2
    with patch("gateway_client.httpx.post", return_value=_response(200, {"ok": True})) as post:
        assert scoped._post("/api/ssh/execute", {"command": "true"}) == {"ok": True}
        assert scoped._post("/api/ssh/execute", {"command": "true"}) == {"ok": True}
    assert post.call_count == 2


def test_changing_auth_during_an_inflight_read_cannot_join_old_identity() -> None:
    client = GatewayClient(base_url="https://gateway.invalid", api_key="auth-a").fork_session()
    first_entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    lock = threading.Lock()

    def fake_get(*args: object, **kwargs: object) -> httpx.Response:
        headers = kwargs["headers"]
        with lock:
            calls.append(headers["X-API-Key"])
        first_entered.set()
        assert release.wait(5), "blocked transport never released"
        return _response(200, {"ready": True})

    with patch("gateway_client.httpx.get", side_effect=fake_get):
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(client.health)
            assert first_entered.wait(3)
            client.api_key = "auth-b"
            second = workers.submit(client.health)
            try:
                time.sleep(0.2)
            finally:
                release.set()
            assert first.result(timeout=3) == {"ready": True}
            assert second.result(timeout=3) == {"ready": True}
    assert sorted(calls) == ["auth-a", "auth-b"]

