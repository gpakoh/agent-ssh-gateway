#!/usr/bin/env python3
"""Idempotently trust the dedicated agent SSH executor in the gateway store.

This runs *inside* the web-ssh-gateway container during deployment.  The API
key is read from that container's environment and is never passed on argv.
The gateway's existing authenticated known-hosts API performs the key scan and
persists the host key; normal SSH connections remain strict and fail closed.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

BASE_URL = "http://127.0.0.1:8085"


def _request(method: str, path: str, *, body: dict[str, object] | None = None) -> dict[str, object]:
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("API_KEY is required inside web-ssh-gateway")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected gateway response for {path}")
    return payload


def main() -> int:
    host = os.environ.get("AGENT_SSH_HOST", "agent-sshd").strip()
    port_text = os.environ.get("AGENT_SSH_PORT", "2222").strip()
    if not host:
        raise RuntimeError("AGENT_SSH_HOST must not be empty")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise RuntimeError("AGENT_SSH_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("AGENT_SSH_PORT must be between 1 and 65535")

    query = urllib.parse.urlencode({"host": host, "port": port})
    check_path = f"/api/known-hosts/check?{query}"
    check = _request("GET", check_path)
    status = check.get("status")
    if status == "unknown":
        _request("POST", "/api/known-hosts", body={"host": host, "port": port})
        check = _request("GET", check_path)
        status = check.get("status")
    if status != "known":
        raise RuntimeError(f"dedicated agent host trust bootstrap failed: status={status!r}")
    print(f"known host verified: {host}:{port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
