#!/usr/bin/env python3
"""Read-only, authenticated browser Kanban release smoke inside web-ssh-gateway.

This script is executed by the existing guarded deploy smoke after the web
container becomes healthy. It neither writes Gitea state nor exposes API/PAT
credentials. When a read-only Gitea PAT is not provisioned, 503 with the
expected fail-closed message is the *required* behavior.
"""

from __future__ import annotations

import http.client
import json
import os
import sys

_TIMEOUT = 45
_MAX_BODY = 65536
_BOARD_PATH = "/api/coordination/board?project=agent-ssh-gateway&limit=1"


def _get(path: str, *, key: str | None = None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", 8085, timeout=_TIMEOUT)
    try:
        headers = {"Accept": "application/json"}
        if key is not None:
            headers["X-API-Key"] = key
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(_MAX_BODY + 1)
        if len(body) > _MAX_BODY:
            raise ValueError("bounded response size exceeded")
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        connection.close()


def _probe(key: str, *, credential_configured: bool) -> None:
    # Unauthenticated access to private board routes must remain forbidden.
    status, _headers, _body = _get("/coordination")
    if status not in (401, 403):
        raise ValueError("unauthenticated board route was not denied")
    status, _headers, _body = _get(_BOARD_PATH)
    if status not in (401, 403):
        raise ValueError("unauthenticated board data was not denied")

    status, headers, body = _get("/coordination", key=key)
    if status != 200 or "text/html" not in headers.get("content-type", ""):
        raise ValueError("authenticated browser route did not serve HTML")
    if b"Py Coordination" not in body or "no-store" not in headers.get("cache-control", ""):
        raise ValueError("browser route content or cache contract mismatch")

    status, headers, body = _get(_BOARD_PATH, key=key)
    try:
        data = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("browser board returned non-JSON data") from exc
    if not isinstance(data, dict):
        raise ValueError("browser board returned an invalid JSON shape")

    if not credential_configured:
        if status != 503 or data.get("detail") != "Coordination read credential is not configured":
            raise ValueError("browser board did not fail closed without read-only credential")
        return

    if status != 200 or data.get("read_only") is not True:
        raise ValueError("configured board failed authenticated read-only probe")
    if "no-store" not in headers.get("cache-control", ""):
        raise ValueError("configured board must not be cached")
    columns = data.get("columns")
    if not isinstance(columns, list) or [c.get("state") for c in columns if isinstance(c, dict)] != [
        "open", "accepted", "waiting_review", "resolved"
    ]:
        raise ValueError("configured board has an unexpected Kanban column contract")
    if data.get("count") not in (0, 1):
        raise ValueError("configured board ignored requested card limit")


def main() -> int:
    key = os.environ.get("API_KEY", "").strip()
    if not key:
        print("coordination_browser_smoke: API_KEY unavailable", file=sys.stderr)
        return 1
    configured = bool(os.environ.get("COORDINATION_GITEA_READ_TOKEN", ""))
    try:
        _probe(key, credential_configured=configured)
    except Exception as exc:
        # Strictly constant diagnostic: never serialize request URL, bearer token,
        # upstream Gitea response, or exception text into CI/deploy logs.
        print(f"coordination_browser_smoke: contract check failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("coordination_browser_smoke: authenticated read-only contract verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
