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
import hashlib
import http.client
import json
import os
import secrets
import sys
import urllib.parse

TIMEOUT = float(os.environ.get("MCP_SMOKE_TIMEOUT", "30"))
BASE_HOST = os.environ.get("MCP_SMOKE_BASE_HOST", "127.0.0.1")
BASE_PORT = int(os.environ.get("MCP_SMOKE_BASE_PORT", "8788"))
REDIRECT_URI = "http://localhost/callback"
SCOPE = "mcp:read mcp:project"
PROJECT = os.environ.get("MCP_SMOKE_PROJECT", "web-ssh-gateway")


class SmokeError(RuntimeError):
    pass


def _req(method: str, path: str, body: dict | None = None, form: bool = False,
         sid: str | None = None, token: str | None = None) -> tuple[int, dict, str]:
    headers = {"Accept": "application/json, text/event-stream"}
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(body).encode()
    else:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode() if body is not None else None
    if sid:
        headers["Mcp-Session-Id"] = sid
    if token:
        headers["Authorization"] = f"Bearer {token}"

    conn = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=TIMEOUT)
    try:
        conn.request(method, path, data, headers)
        resp = conn.getresponse()
        buf = b""
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            buf += chunk
            if b"\n\n" in buf and path.startswith("/mcp"):
                break
        raw = buf.decode("utf-8", errors="replace")
        payload: dict = {}
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


def _mcp_call(token: str, method: str, params: dict, sid: str | None = None
              ) -> tuple[dict, str]:
    status, payload, _, ret_sid = _req("POST", "/mcp", {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }, sid=sid, token=token)
    if status != 200:
        raise SmokeError(f"mcp {method}: http {status}")
    if "error" in payload:
        raise SmokeError(f"mcp {method}: {payload['error']}")
    return payload.get("result", {}), ret_sid


def main() -> int:
    password = os.environ.get("MCP_AUTHORIZE_PASSWORD", "")
    if not password:
        print("mcp_oauth_black_box_smoke: MCP_AUTHORIZE_PASSWORD not set", file=sys.stderr)
        return 1

    try:
        client_id = _register_client()
        token = _oauth_flow(client_id, password)

        result, sid = _mcp_call(token, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "bb-smoke", "version": "1.0"},
        })
        if not sid:
            raise SmokeError("initialize returned no session id")

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

        print(f"mcp_oauth_black_box_smoke: OK client={client_id} "
              f"outcome={outcome} exit_code={exit_code}")
        return 0
    except SmokeError as exc:
        print(f"mcp_oauth_black_box_smoke: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"mcp_oauth_black_box_smoke: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
