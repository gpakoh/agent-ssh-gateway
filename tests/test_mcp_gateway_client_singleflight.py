"""Bounded, session-local in-flight HTTP health-read coalescing contract."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

MCP_SERVER_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
if str(MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_SERVER_DIR))

from gateway_client import GatewayClient, GatewayClientError  # noqa: E402


def _response(
    status: int, payload: dict[str, object], url: str = "https://gateway.invalid/health"
) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        headers={"Retry-After": "7", "X-RateLimit-Bucket": "ip"}
        if status == 429
        else {},
        request=httpx.Request("GET", url),
    )


def _run_overlapping_read(
    read: Callable[[], object], response: httpx.Response
) -> tuple[list[object], int]:
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
        return read()

    with patch("gateway_client.httpx.get", side_effect=fake_get):
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(read)
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


def _run_overlapping_health(
    client: GatewayClient, response: httpx.Response
) -> tuple[list[object], int]:
    return _run_overlapping_read(client.health, response)


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("health", {"ready": True}),
        ("list_sessions", {"sessions": []}),
    ],
)
def test_cross_transport_session_independent_reads_share_only_one_inflight_call(
    method: str, payload: dict[str, object]
) -> None:
    # Two independent MCP lifecycles use separate scoped clients/SIDs but
    # exactly the same base URL and API authority. Neither endpoint depends on
    # the selected session ID, so concurrent reads should share one request.
    base = GatewayClient(base_url="https://gateway.invalid", api_key="same-auth")
    first, second = base.fork_session(), base.fork_session()
    first.session_id, second.session_id = "session-a", "session-b"
    order = iter((first, second))
    select_lock = threading.Lock()

    def read() -> object:
        with select_lock:
            client = next(order)
        return getattr(client, method)()

    results, calls = _run_overlapping_read(read, _response(200, payload))
    assert results == [payload, payload]
    assert calls == 1

    # Only the concurrent flight is shared. There is no cross-transport cache.
    with patch("gateway_client.httpx.get", return_value=_response(200, payload)) as get:
        assert (first.health() if method == "health" else first.list_sessions()) == payload
        assert (second.health() if method == "health" else second.list_sessions()) == payload
    assert get.call_count == 2


def test_cross_transport_429_is_shared_but_bounded_and_not_cached() -> None:
    base = GatewayClient(base_url="https://gateway.invalid", api_key="same-auth")
    first, second = base.fork_session(), base.fork_session()
    first.session_id, second.session_id = "different-a", "different-b"
    order = iter((first, second))
    select_lock = threading.Lock()

    def read() -> object:
        with select_lock:
            client = next(order)
        return client.health()

    results, calls = _run_overlapping_read(
        read,
        _response(
            429,
            {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "do not return secret auth token",
                "details": {"private": "secret", "retry_after_seconds": 9},
            },
        ),
    )
    assert calls == 1
    for result in results:
        assert isinstance(result, GatewayClientError)
        assert result.status_code == 429
        assert result.body == {
            "code": "RATE_LIMIT_EXCEEDED",
            "message": "Gateway rate limit response",
            "retryable": True,
            "details": {"retry_after_seconds": 7, "bucket_class": "ip"},
        }
        assert "secret" not in str(result)
    # Separate logical clients must not share a mutable exception instance,
    # even when the underlying 429 response was de-duplicated.
    assert results[0] is not results[1]
    assert isinstance(results[0], GatewayClientError)
    assert isinstance(results[1], GatewayClientError)
    assert isinstance(results[0].body, dict)
    results[0].body["details"]["retry_after_seconds"] = 99
    assert results[1].body["details"]["retry_after_seconds"] == 7
    with patch(
        "gateway_client.httpx.get",
        return_value=_response(200, {"ready": True}),
    ) as get:
        assert first.health() == {"ready": True}
        assert second.health() == {"ready": True}
    assert get.call_count == 2


@pytest.mark.parametrize(
    "isolator", ["api_key", "base_url", "session_bound_path", "timeout"]
)
def test_cross_transport_flights_do_not_cross_security_or_session_bounds(
    isolator: str,
) -> None:
    first = GatewayClient(
        base_url="https://gateway.invalid", api_key="auth-a"
    ).fork_session()
    second = GatewayClient(
        base_url="https://gateway.invalid", api_key="auth-a"
    ).fork_session()
    first.session_id, second.session_id = "different-a", "different-b"
    if isolator == "api_key":
        second.api_key = "auth-b"
    if isolator == "base_url":
        second.base_url = "https://other-gateway.invalid"
    order = iter((first, second))
    select_lock = threading.Lock()

    def read() -> object:
        with select_lock:
            client = next(order)
        if isolator == "session_bound_path":
            return client.job_status("job-1")
        if isolator == "timeout":
            return client._get("/health", timeout=10 if client is first else 20)
        return client.health()

    results, calls = _run_overlapping_read(
        read, _response(200, {"status": "ok"})
    )
    assert results == [{"status": "ok"}, {"status": "ok"}]
    assert calls == 2


def test_cross_transport_flight_table_saturation_falls_back_without_allocating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A flood of distinct authenticated readers must not grow the
    # process-global in-flight table without bound.
    first_key = (
        "https://gateway.invalid", "other-auth", "",
        "/health", (), 30,
    )
    in_flight: dict[tuple[object, ...], object] = {first_key: object()}
    monkeypatch.setattr(GatewayClient, "_shared_get_inflight", in_flight)
    monkeypatch.setattr(GatewayClient, "_SHARED_GET_MAX_INFLIGHT", 1)
    client = GatewayClient(
        base_url="https://gateway.invalid", api_key="new-auth"
    ).fork_session()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        assert url == "https://gateway.invalid/health"
        assert kwargs["headers"] == {"X-API-Key": "new-auth"}
        assert set(in_flight) == {first_key}
        assert len(in_flight) == 1
        return _response(200, {"ready": True}, url=url)

    with patch("gateway_client.httpx.get", side_effect=fake_get) as get:
        assert client.health() == {"ready": True}
        assert client.health() == {"ready": True}
    assert get.call_count == 2
    assert set(in_flight) == {first_key}


def test_cross_transport_existing_flight_can_join_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ("https://gateway.invalid", "same-auth", "", "/health", (), 30)
    published: Future[dict[str, object]] = Future()
    published.set_result({"ready": True})
    flights = {key: published}
    monkeypatch.setattr(GatewayClient, "_shared_get_inflight", flights)
    monkeypatch.setattr(GatewayClient, "_SHARED_GET_MAX_INFLIGHT", 1)
    client = GatewayClient(
        base_url="https://gateway.invalid", api_key="same-auth"
    ).fork_session()
    with patch("gateway_client.httpx.get") as get:
        assert client.health() == {"ready": True}
    get.assert_not_called()
    assert flights == {key: published}


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
    assert all(
        result.body["details"] == {"retry_after_seconds": 7, "bucket_class": "ip"}
        for result in results
    )
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


@pytest.mark.parametrize(
    ("read", "payload", "expected"),
    [
        (lambda client: client.list_sessions(), {"sessions": []}, {"sessions": []}),
        (lambda client: client.session_health(), {"healthy": True}, {"healthy": True}),
        (
            lambda client: client.job_status("job-1"),
            {"status": "completed"},
            {"status": "completed"},
        ),
        (
            lambda client: client.job_result("job-1"),
            {"status": "completed"},
            {"status": "completed"},
        ),
        (
            lambda client: client.resolve_submission_job("submission-1"),
            {"job_id": "job-1"},
            "job-1",
        ),
        (
            lambda client: client.resolve_submission_job_family("prefix-1"),
            {"job_id": "job-2"},
            "job-2",
        ),
    ],
)
def test_scoped_status_reads_share_only_inflight_work(read, payload, expected) -> None:
    client = GatewayClient(base_url="https://gateway.invalid", api_key="same-auth").fork_session()
    client.session_id = "session-1"
    results, calls = _run_overlapping_read(lambda: read(client), _response(200, payload))
    assert results == [expected, expected]
    assert calls == 1
    with patch("gateway_client.httpx.get", return_value=_response(200, payload)) as get:
        assert read(client) == expected
    get.assert_called_once()


def test_scoped_job_result_redaction_mode_is_part_of_flight_key() -> None:
    client = GatewayClient(base_url="https://gateway.invalid", api_key="same-auth").fork_session()
    entered = threading.Event()
    two_calls = threading.Event()
    release = threading.Event()
    modes: list[str] = []
    lock = threading.Lock()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        params = kwargs["params"]
        assert isinstance(params, dict)
        with lock:
            modes.append(params["redact_output"])
            if len(modes) == 2:
                two_calls.set()
        entered.set()
        assert release.wait(5), "test transport was not released"
        return _response(200, {"redact_output": params["redact_output"]}, url=url)

    with patch("gateway_client.httpx.get", side_effect=fake_get):
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(client.job_result, "job-1", True)
            assert entered.wait(3)
            second = workers.submit(client.job_result, "job-1", False)
            try:
                assert two_calls.wait(3), "different query must not join one flight"
            finally:
                release.set()
            assert first.result(timeout=3) == {"redact_output": "true"}
            assert second.result(timeout=3) == {"redact_output": "false"}
    assert sorted(modes) == ["false", "true"]


def test_job_wait_long_poll_is_never_coalesced() -> None:
    client = GatewayClient(base_url="https://gateway.invalid", api_key="same-auth").fork_session()
    results, calls = _run_overlapping_read(
        lambda: client.wait_job("job-1", timeout_sec=1),
        _response(200, {"status": "completed", "exit_code": 0}),
    )
    assert results == [
        {"status": "completed", "exit_code": 0},
        {"status": "completed", "exit_code": 0},
    ]
    assert calls == 2


def test_scoped_session_health_does_not_join_a_different_session_id() -> None:
    client = GatewayClient(base_url="https://gateway.invalid", api_key="same-auth").fork_session()
    client.session_id = "session-a"
    entered = threading.Event()
    two_calls = threading.Event()
    release = threading.Event()
    urls: list[str] = []
    lock = threading.Lock()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        with lock:
            urls.append(url)
            if len(urls) == 2:
                two_calls.set()
        entered.set()
        assert release.wait(5), "test transport was not released"
        return _response(200, {"healthy": True}, url=url)

    with patch("gateway_client.httpx.get", side_effect=fake_get):
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(client.session_health)
            assert entered.wait(3)
            client.session_id = "session-b"
            second = workers.submit(client.session_health)
            try:
                assert two_calls.wait(3), "different session id must not join one flight"
            finally:
                release.set()
            assert first.result(timeout=3) == {"healthy": True}
            assert second.result(timeout=3) == {"healthy": True}
    assert sorted(urls) == [
        "https://gateway.invalid/api/ssh/session/session-a/health",
        "https://gateway.invalid/api/ssh/session/session-b/health",
    ]


def test_follower_timeout_does_not_cancel_or_poison_inflight_status_read() -> None:
    client = GatewayClient(base_url="https://gateway.invalid", api_key="same-auth").fork_session()
    path = "/api/jobs/job-1/status"
    entered = threading.Event()
    release = threading.Event()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        entered.set()
        assert release.wait(5), "test transport was not released"
        return _response(200, {"status": "completed"}, url=url)

    with patch("gateway_client.httpx.get", side_effect=fake_get):
        with ThreadPoolExecutor(max_workers=2) as workers:
            leader = workers.submit(client._get, path, None, 0.05)
            assert entered.wait(3)
            follower = workers.submit(client._get, path, None, 0.05)
            try:
                with pytest.raises(GatewayClientError) as exc:
                    follower.result(timeout=2)
                assert exc.value.body["code"] == "TIMEOUT"
            finally:
                release.set()
            assert leader.result(timeout=3) == {"status": "completed"}

    with patch(
        "gateway_client.httpx.get",
        return_value=_response(200, {"status": "fresh"}),
    ) as get:
        assert client._get(path, None, 0.05) == {"status": "fresh"}
    get.assert_called_once()
