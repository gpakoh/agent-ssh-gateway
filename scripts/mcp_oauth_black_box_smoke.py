#!/usr/bin/env python3
"""Authenticated black-box smoke for the deployed mcp-oauth container:
full DCR/OAuth authorization-code flow -> real tools/call git_status.

P1 audit finding #6 (host-smoke SSH/OAuth gap): the post-deploy checks
only ever ran initialize + tools/list (mcp_black_box_smoke.py via bearer
token, mcp_oauth_healthcheck.py via the healthcheck token) -- neither
exercised a real SSH-executing tool. git_status is the only safe-profile
tool (scope mcp:project, present in mcp_client_safe) that really runs a
harmless SSH command through the Gateway (/api/ssh/execute-argv -> sshd
-> git status --short). This drives the whole live chain end to end:

  /register (DCR) -> /authorize (PKCE S256) -> /oauth/consent (password)
  -> /token (authorization_code) -> initialize -> tools/call git_status
  -> exact result (isError=false, exit_code=0, git status --short)

The consent password comes from MCP_AUTHORIZE_PASSWORD (container env);
the SSH auto-reconnect uses GATEWAY_SSH_* + GATEWAY_SSH_KEY_PATH. Run
inside the mcp-oauth container via `docker exec mcp-oauth python3
scripts/mcp_oauth_black_box_smoke.py` (127.0.0.1:8788 is the public
proxy) or on the host against 127.0.0.1:8788 with the same env.

Exit code:
    0 - full flow completed, git_status returned isError=false and
        exit_code=0 with non-empty stdout
    1 - missing env, transport error, consent/token failure, JSON-RPC
        error, or git_status did not produce the exact result
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import json
import os
import secrets
import signal
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from typing import Any

TIMEOUT = float(os.environ.get("MCP_SMOKE_TIMEOUT", "90"))
BASE_HOST = os.environ.get("MCP_SMOKE_BASE_HOST", "127.0.0.1")
BASE_PORT = int(os.environ.get("MCP_SMOKE_BASE_PORT", "8788"))
REDIRECT_URI = "http://localhost/callback"
SCOPE = "mcp:read mcp:project"
DEFAULT_PROJECT = "agent-ssh-gateway"
PROJECT = os.environ.get("MCP_SMOKE_PROJECT", DEFAULT_PROJECT)


class SmokeError(RuntimeError):
    pass


def _required_schema() -> tuple[str, tuple[str, ...]]:
    tool_name = os.environ.get("MCP_SMOKE_REQUIRED_TOOL", "").strip()
    raw_inputs = os.environ.get("MCP_SMOKE_REQUIRED_INPUTS", "")
    inputs = tuple(dict.fromkeys(item.strip() for item in raw_inputs.split(",") if item.strip()))
    if inputs and not tool_name:
        raise SmokeError("MCP_SMOKE_REQUIRED_INPUTS requires MCP_SMOKE_REQUIRED_TOOL")
    return tool_name, inputs


def _validate_required_tool_schema(tools: Any) -> None:
    tool_name, required_inputs = _required_schema()
    if not tool_name:
        return
    if not isinstance(tools, list):
        raise SmokeError("tools/list returned no tools array")

    matches = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") == tool_name]
    if len(matches) != 1:
        raise SmokeError(f"required tool {tool_name!r} missing or ambiguous")
    schema = matches[0].get("inputSchema")
    if not isinstance(schema, dict):
        raise SmokeError(f"required tool {tool_name!r} has no inputSchema object")
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise SmokeError(f"required tool {tool_name!r} has incomplete inputSchema")

    missing_properties = [name for name in required_inputs if name not in properties]
    missing_required = [name for name in required_inputs if name not in required]
    if missing_properties or missing_required:
        raise SmokeError(
            f"required tool {tool_name!r} schema mismatch: "
            f"missing properties={missing_properties}, missing required={missing_required}"
        )


MAX_SMOKE_RESPONSE_BYTES = 256 * 1024


def _has_complete_sse_data_event(buffer: bytearray) -> bool:
    """Ignore completed SSE comment/keepalive frames before the JSON-RPC data."""
    normalized = bytes(buffer).replace(b"\r\n", b"\n")
    return any(
        any(line.startswith(b"data:") for line in frame.split(b"\n"))
        for frame in normalized.split(b"\n\n")[:-1]
    )


def _read_response_body(resp: http.client.HTTPResponse) -> bytes:
    """Read a finite JSON body or one SSE event from a persistent MCP stream.

    HTTPResponse.read(n) can wait to fill n bytes on a non-terminating
    event-stream. read1(n) returns data from one socket read instead, so a
    short SSE event is actionable without waiting for the connection to close.
    Fail closed on incomplete/oversize responses.
    """
    kind = (resp.getheader("Content-Type", "") or "").lower()
    is_sse = "text/event-stream" in kind
    read_chunk = resp.read1 if is_sse else resp.read
    chunks = bytearray()
    while True:
        chunk = read_chunk(8192)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_SMOKE_RESPONSE_BYTES:
            raise SmokeError("MCP smoke response exceeds byte limit")
        if is_sse and _has_complete_sse_data_event(chunks):
            return bytes(chunks)
    if is_sse:
        raise SmokeError("MCP smoke SSE stream ended before a complete event")
    return bytes(chunks)


# A single stalled HTTP request must not consume the host-smoke's entire
# subprocess deadline. Leave time for bounded session cleanup and diagnostics.
MAX_HTTP_STAGE_SECONDS = 20.0
MAX_CLEANUP_SECONDS = 8.0
_SMOKE_DEADLINE: float | None = None
_LAST_STAGE = "not_started"


def _http_stage(method: str, path: str, body: dict[str, Any] | None) -> str:
    """Return only a fixed, non-secret stage name (never reflect URLs)."""
    if method == "POST" and path == "/register":
        return "register"
    if method == "GET" and path.startswith("/authorize?"):
        return "authorize"
    if method == "POST" and path == "/oauth/consent":
        return "consent"
    if method == "POST" and path == "/token":
        return "token"
    if method == "DELETE" and path == "/mcp":
        return "cleanup"
    if method == "POST" and path == "/mcp":
        raw_name = (body or {}).get("method")
        name = raw_name if isinstance(raw_name, str) else ""
        return {
            "initialize": "initialize",
            "tools/list": "tools_list",
            "tools/call": "git_status",
        }.get(name, "mcp_other")
    return "unknown"


def _request_timeout(stage: str) -> float:
    """One socket step cannot monopolize the whole black-box smoke budget."""
    if stage == "cleanup":
        return min(TIMEOUT, MAX_CLEANUP_SECONDS)
    remaining = (
        TIMEOUT if _SMOKE_DEADLINE is None
        else _SMOKE_DEADLINE - time.monotonic()
    )
    if remaining <= 0:
        raise SmokeError("smoke request deadline exhausted")
    return min(TIMEOUT, MAX_HTTP_STAGE_SECONDS, remaining)


@contextlib.contextmanager
def _bounded_http_stage(seconds: float) -> Iterator[None]:
    """Enforce a hard POSIX wall-clock bound, even on slow-dripping streams.

    HTTPConnection timeout controls each socket operation, not the whole
    request/response exchange. The smoke runs on Linux, in its own main-thread
    process; refuse a conflicting alarm rather than override another owner.
    """
    if threading.current_thread() is not threading.main_thread():
        raise SmokeError("HTTP smoke stage timer requires main thread")
    if signal.getitimer(signal.ITIMER_REAL)[0] > 0:
        raise SmokeError("HTTP smoke stage timer conflict")
    old_handler = signal.getsignal(signal.SIGALRM)

    def _expire(_signal_number: int, _frame: Any) -> None:
        raise TimeoutError("MCP smoke HTTP stage wall-clock limit exceeded")

    signal.signal(signal.SIGALRM, _expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def _req(method: str, path: str, body: dict[str, Any] | None = None, form: bool = False,
         sid: str | None = None, token: str | None = None) -> tuple[int, dict[str, Any], str, str]:
    headers = {"Accept": "application/json, text/event-stream"}
    data: bytes | None
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(body or {}).encode()
    else:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode() if body is not None else None
    if sid:
        headers["Mcp-Session-Id"] = sid
    if token:
        headers["Authorization"] = f"Bearer {token}"

    global _LAST_STAGE
    stage = _http_stage(method, path, body)
    _LAST_STAGE = stage
    # Flush before a potentially blocking socket call: TimeoutExpired should
    # retain the last static phase without leaking tokens, URLs or bodies.
    print(f"mcp_oauth_black_box_smoke: stage={stage}", file=sys.stderr, flush=True)
    budget = _request_timeout(stage)
    conn = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=budget)
    try:
        with _bounded_http_stage(budget):
            conn.request(method, path, data, headers)
            resp = conn.getresponse()
            raw = _read_response_body(resp).decode("utf-8", errors="replace")
        payload: dict[str, Any] = {}
        for line in raw.split("\n"):
            if line.startswith("data:"):
                payload = json.loads(line[5:])
                break
        if not payload and raw.strip().startswith("{"):
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                pass
        status = resp.status
        location = resp.getheader("Location", "")
        ret_sid = resp.getheader("mcp-session-id", "")
        resp.close()
        return status, payload, location, ret_sid
    finally:
        conn.close()


def _register_client() -> str:
    status, payload, _, _ = _req("POST", "/register", {
        "client_name": f"bb-smoke-{secrets.token_hex(4)}",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    })
    if status != 201 or "client_id" not in payload:
        raise SmokeError(f"register failed: {status} {payload}")
    return payload["client_id"]


def _oauth_flow(client_id: str, password: str) -> str:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": "bb-smoke",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    status, _, location, _ = _req("GET", "/authorize?" + qs)
    if status not in (302, 303) or not location:
        raise SmokeError(f"authorize failed: {status} {location}")

    consent_path = urllib.parse.urlparse(location).path or "/oauth/consent"
    qp = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    status, _, location, _ = _req("POST", consent_path, {
        "password": password,
        "client_id": qp.get("client_id", [""])[0],
        "redirect_uri": qp.get("redirect_uri", [""])[0],
        "scope": qp.get("scope", [""])[0],
        "state": qp.get("state", [""])[0],
        "code_challenge": qp.get("code_challenge", [""])[0],
        "resource": qp.get("resource", [""])[0],
    }, form=True)
    if status not in (302, 303) or not location:
        raise SmokeError(f"consent failed: {status} {location}")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [""])[0]
    if not code:
        raise SmokeError(f"consent produced no code: {location}")

    status, payload, _, _ = _req("POST", "/token", {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }, form=True)
    if status != 200 or "access_token" not in payload:
        raise SmokeError(f"token failed: {status} {payload}")
    return payload["access_token"]


def _mcp_call(token: str, method: str, params: dict[str, Any], sid: str | None = None
              ) -> tuple[dict[str, Any], str]:
    status, payload, _, ret_sid = _req("POST", "/mcp", {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }, sid=sid, token=token)
    if status != 200:
        raise SmokeError(f"mcp {method}: http {status}")
    if not payload:
        raise SmokeError(f"mcp {method}: empty or malformed response")
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != 1:
        raise SmokeError(f"mcp {method}: response id/jsonrpc mismatch")
    if "error" in payload:
        raise SmokeError(f"mcp {method}: {payload['error']}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise SmokeError(f"mcp {method}: missing result object")
    return result, ret_sid


def _terminate_session(token: str, sid: str) -> None:
    status, _, _, _ = _req("DELETE", "/mcp", sid=sid, token=token)
    if status not in (200, 204):
        raise SmokeError(f"mcp session termination: http {status}")


def main() -> int:
    password = os.environ.get("MCP_AUTHORIZE_PASSWORD", "")
    if not password:
        print("mcp_oauth_black_box_smoke: MCP_AUTHORIZE_PASSWORD not set", file=sys.stderr)
        return 1

    client_id = ""
    token = ""
    sid = ""
    outcome: Any = None
    exit_code: Any = None
    rc = 1
    global _SMOKE_DEADLINE, _LAST_STAGE
    previous_deadline = _SMOKE_DEADLINE
    _SMOKE_DEADLINE = time.monotonic() + max(
        0.1, TIMEOUT - min(10.0, TIMEOUT / 5)
    )
    _LAST_STAGE = "not_started"
    try:
        required_tool, _ = _required_schema()
        client_id = _register_client()
        token = _oauth_flow(client_id, password)

        result, sid = _mcp_call(token, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "bb-smoke", "version": "1.0"},
        })
        if not sid:
            raise SmokeError("initialize returned no session id")

        if required_tool:
            tools_result, _ = _mcp_call(token, "tools/list", {}, sid=sid)
            _validate_required_tool_schema(tools_result.get("tools"))

        result, _ = _mcp_call(token, "tools/call", {
            "name": "git_status",
            "arguments": {"project": PROJECT},
        }, sid=sid)

        if result.get("isError"):
            raise SmokeError(f"git_status isError: {result}")
        content = result.get("content") or []
        text = "".join(
            item.get("text", "") for item in content if item.get("type") == "text"
        )
        payload = {}
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            pass
        result_data = payload.get("result", {})
        exit_code = result_data.get("exit_code")
        outcome = result_data.get("outcome")
        # git status --short on a clean checkout is EMPTY stdout -- the
        # exact result is that the SSH chain really ran: outcome=passed
        # + exit_code=0 (executed via /api/ssh/execute-argv -> sshd).
        if outcome != "passed" or exit_code != 0:
            raise SmokeError(f"git_status unexpected result: {payload}")
        rc = 0
    except SmokeError:
        # Error payloads (including raw upstream JSON) are not safe to echo.
        print(f"mcp_oauth_black_box_smoke: stage={_LAST_STAGE} error=SmokeError",
              file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"mcp_oauth_black_box_smoke: stage={_LAST_STAGE} "
              f"error={type(exc).__name__}", file=sys.stderr, flush=True)
    finally:
        if token and sid:
            try:
                _terminate_session(token, sid)
            except Exception as exc:
                print(
                    f"mcp_oauth_black_box_smoke: session cleanup failed: "
                    f"{type(exc).__name__}",
                    file=sys.stderr, flush=True,
                )
                rc = 1
        _SMOKE_DEADLINE = previous_deadline

    if rc == 0:
        print(f"mcp_oauth_black_box_smoke: OK client={client_id} "
              f"outcome={outcome} exit_code={exit_code}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
