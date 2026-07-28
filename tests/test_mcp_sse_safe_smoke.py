"""Tests for scripts/mcp_sse_safe_smoke.py — SSE runtime smoke test.

No real network or subprocess is used: HTTP checks are exercised
against a fake httpx.Client (monkeypatched), subprocess management is
tested against a fake Popen-like object, and env construction is
inspected as a plain dict. The real end-to-end run (real subprocess,
real socket, real MCP-over-SSE protocol) is exercised manually via
`python3 scripts/mcp_sse_safe_smoke.py`, per the task's validation step
— not duplicated here as a slow/flaky pytest case.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER_DIR = ROOT / "examples" / "mcp_server"
for _p in (str(MCP_SERVER_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scripts.mcp_sse_safe_smoke as smoke_module  # noqa: E402
from scripts.mcp_sse_safe_smoke import (  # noqa: E402
    build_env,
    check,
    check_http_auth,
    find_free_port,
    generate_test_token,
    stop_server,
)

SCRIPT_SOURCE = (ROOT / "scripts" / "mcp_sse_safe_smoke.py").read_text()


# ---------------------------------------------------------------------------
# Env construction — no token leakage
# ---------------------------------------------------------------------------


class TestBuildEnv:
    def test_token_only_in_bearer_key(self):
        token = "super-secret-test-token-value"
        env = build_env("127.0.0.1", 12345, token)
        leaking_keys = [k for k, v in env.items() if v == token and k != "MCP_HTTP_BEARER_TOKEN"]
        assert env["MCP_HTTP_BEARER_TOKEN"] == token
        assert leaking_keys == []

    def test_safe_mode_env_present(self):
        env = build_env("127.0.0.1", 12345, "tok")
        assert env["MCP_GATEWAY_TOOL_MODE"] == "mcp_client"
        assert env["MCP_CLIENT_SAFE_MODE"] == "true"
        assert env["MCP_ACCESS_PROFILE"] == "mcp_client_safe"

    def test_gateway_env_passthrough_only_when_present(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GATEWAY_URL", None)
            os.environ.pop("GATEWAY_AGENT_TOKEN", None)
            env = build_env("127.0.0.1", 12345, "tok")
            assert "GATEWAY_URL" not in env
            assert "GATEWAY_AGENT_TOKEN" not in env

    def test_gateway_env_passthrough_when_set(self):
        with patch.dict(os.environ, {"GATEWAY_URL": "http://example.invalid", "GATEWAY_AGENT_TOKEN": "gw-tok"}):
            env = build_env("127.0.0.1", 12345, "tok")
            assert env["GATEWAY_URL"] == "http://example.invalid"
            assert env["GATEWAY_AGENT_TOKEN"] == "gw-tok"
            assert env["GATEWAY_API_KEY"] == "gw-tok"

    def test_does_not_mutate_parent_os_environ(self):
        before = dict(os.environ)
        build_env("127.0.0.1", 12345, "some-token-that-must-not-leak")
        assert os.environ == before  # returns a fresh dict, never mutates os.environ


class TestGenerateTestToken:
    def test_reasonably_unique_and_nonempty(self):
        a = generate_test_token()
        b = generate_test_token()
        assert a and b
        assert a != b
        assert len(a) >= 20


class TestFindFreePort:
    def test_returns_bindable_port(self):
        port = find_free_port()
        assert isinstance(port, int)
        assert 1024 < port < 65536


# ---------------------------------------------------------------------------
# Subprocess cleanup path — fake Popen, no real process
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, already_exited: bool = False, hangs: bool = False):
        self._already_exited = already_exited
        self._hangs = hangs
        self.terminate_called = False
        self.kill_called = False
        self._wait_calls = 0

    def poll(self):
        return 0 if self._already_exited else None

    def terminate(self):
        self.terminate_called = True

    def kill(self):
        self.kill_called = True

    def wait(self, timeout=None):
        self._wait_calls += 1
        if self._hangs and not self.kill_called:
            import subprocess

            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return 0


class TestStopServer:
    def test_already_exited_process_is_left_alone(self):
        proc = _FakeProc(already_exited=True)
        stop_server(proc)
        assert proc.terminate_called is False

    def test_running_process_is_terminated(self):
        proc = _FakeProc(already_exited=False)
        stop_server(proc)
        assert proc.terminate_called is True
        assert proc.kill_called is False

    def test_hanging_process_is_killed_after_terminate_timeout(self):
        proc = _FakeProc(already_exited=False, hangs=True)
        stop_server(proc)
        assert proc.terminate_called is True
        assert proc.kill_called is True


# ---------------------------------------------------------------------------
# check() / PASS/FAIL counter logic
# ---------------------------------------------------------------------------


class TestCheckCounter:
    def test_pass_increments_pass_and_prints_checkmark(self):
        smoke_module.PASS = 0
        smoke_module.FAIL = 0
        out = io.StringIO()
        with redirect_stdout(out):
            check("something ok", True, "detail here")
        assert smoke_module.PASS == 1
        assert smoke_module.FAIL == 0
        assert "✅" in out.getvalue()
        assert "detail here" in out.getvalue()

    def test_fail_increments_fail_and_prints_cross(self):
        smoke_module.PASS = 0
        smoke_module.FAIL = 0
        out = io.StringIO()
        with redirect_stdout(out):
            check("something bad", False, "why it failed")
        assert smoke_module.PASS == 0
        assert smoke_module.FAIL == 1
        assert "❌" in out.getvalue()


# ---------------------------------------------------------------------------
# HTTP auth checks against an in-memory ASGI app — no real network
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeHttpxClient:
    """Stand-in for httpx.Client that resolves canned status codes based
    on the requested URL + Authorization header, and records every URL
    requested — so tests can assert check_http_auth() only ever hits
    /sse and /messages/, never a guessed path like /mcp/sse, without any
    real socket or ASGI transport plumbing involved.
    """

    ROUTES = {"/sse", "/messages/"}

    def __init__(self, expected_token: str, requested_urls: list[str], **_kwargs: object):
        self._expected_token = expected_token
        self._requested_urls = requested_urls

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def _status_for(self, url: str, headers: dict[str, str] | None) -> int:
        self._requested_urls.append(url)
        path = url.split("http://testserver", 1)[-1]
        if path not in self.ROUTES:
            return 404
        auth = (headers or {}).get("Authorization", "")
        return 200 if auth == f"Bearer {self._expected_token}" else 401

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        return _FakeResponse(self._status_for(url, headers))

    def post(self, url: str, json: object = None, headers: dict[str, str] | None = None) -> _FakeResponse:
        return _FakeResponse(self._status_for(url, headers))

    def stream(self, method: str, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        return _FakeResponse(self._status_for(url, headers))


class TestCheckHttpAuthTargetsRealRoutes:
    """Proves check_http_auth() requests /sse and /messages/ — not a
    guessed path like /mcp/sse — by monkeypatching httpx.Client with a
    fake that resolves status codes from the requested URL. No real
    socket, subprocess, or ASGI transport involved.
    """

    def test_all_auth_checks_pass_and_hit_expected_paths(self, monkeypatch):
        token = "matching-token"
        requested_urls: list[str] = []

        monkeypatch.setattr(
            httpx, "Client",
            lambda **kw: _FakeHttpxClient(token, requested_urls, **kw),
        )

        smoke_module.PASS = 0
        smoke_module.FAIL = 0
        out = io.StringIO()
        with redirect_stdout(out):
            check_http_auth("http://testserver", token)

        assert smoke_module.FAIL == 0
        assert smoke_module.PASS == 4  # no-token, wrong-token, messages-no-token, correct-token

        paths_hit = {u.split("http://testserver", 1)[-1] for u in requested_urls}
        assert paths_hit == {"/sse", "/messages/"}
        assert "/mcp/sse" not in paths_hit

    def test_fails_loudly_if_path_were_wrong(self, monkeypatch):
        """If check_http_auth ever regressed to requesting a path our
        fake gateway doesn't recognize (e.g. /mcp/sse), the fake
        returns 404 — not 401 — and the "no token -> 401" assertion
        inside check_http_auth must register as a failed check, not a
        silently-passing one.
        """
        token = "matching-token"
        requested_urls: list[str] = []

        class _WrongPathClient(_FakeHttpxClient):
            ROUTES = {"/mcp/sse", "/mcp/messages/"}  # deliberately wrong

        monkeypatch.setattr(
            httpx, "Client",
            lambda **kw: _WrongPathClient(token, requested_urls, **kw),
        )

        smoke_module.PASS = 0
        smoke_module.FAIL = 0
        out = io.StringIO()
        with redirect_stdout(out):
            check_http_auth("http://testserver", token)

        assert smoke_module.FAIL > 0


# ---------------------------------------------------------------------------
# Static source checks — correct routes/env-var names, no stale guesses
# ---------------------------------------------------------------------------


class TestScriptSourceReferencesCorrectNames:
    def test_references_sse_and_messages_paths(self):
        assert '/sse' in SCRIPT_SOURCE
        assert '/messages' in SCRIPT_SOURCE

    def test_does_not_functionally_use_old_bind_public_env_var(self):
        """The old MCP_HTTP_BIND_PUBLIC name may legitimately appear in
        the module docstring as a contrast against the real
        MCP_HTTP_ALLOW_NON_LOOPBACK name (documentation, not a bug) —
        so this checks functional usage, not raw substring absence: the
        built env dict must never set MCP_HTTP_BIND_PUBLIC, and the
        source must not read it via os.environ.
        """
        env = build_env("127.0.0.1", 12345, "tok")
        assert "MCP_HTTP_BIND_PUBLIC" not in env
        assert 'os.environ.get("MCP_HTTP_BIND_PUBLIC"' not in SCRIPT_SOURCE
        assert 'os.environ["MCP_HTTP_BIND_PUBLIC"]' not in SCRIPT_SOURCE

    def test_never_sets_allow_non_loopback(self):
        """This smoke script only ever runs the server on 127.0.0.1, so
        it must never set MCP_HTTP_ALLOW_NON_LOOPBACK in the env it
        builds for the subprocess.
        """
        env = build_env("127.0.0.1", 12345, "tok")
        assert "MCP_HTTP_ALLOW_NON_LOOPBACK" not in env

    def test_mentions_correct_non_loopback_env_var_name_in_docs(self):
        # Mentioned in the module docstring for operator context, even
        # though this script itself never sets it.
        assert "MCP_HTTP_ALLOW_NON_LOOPBACK" in SCRIPT_SOURCE


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
