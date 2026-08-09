#!/usr/bin/env python3
"""Authenticated black-box smoke check for the deployed web-ssh-gateway
container, run via `docker exec web-ssh-gateway python3` from
deploy-from-registry.sh after a real deploy.

P1 BLOCKER audit finding: the only post-deploy check was
wait_docker_health() (docker inspect's own HEALTHCHECK status) --
readiness of the process, not proof the actual API surface works. This
does what the audit itself suggested: authenticated client -> a real
read-only operation through the X-API-Key boundary -> exact response
shape validation, not just "got a 200".

GET /api/ssh/sessions (unlike /health and /api/capabilities, which are
both explicitly public -- see app/main.py) requires a valid X-API-Key
with the ssh:execute scope, so a successful call here proves the auth
middleware, not just the process, is actually working end to end.

Exit code:
    0 - authenticated request succeeded with the expected response shape
    1 - missing API_KEY, request failed, or the response didn't match
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = float(os.environ.get("GATEWAY_SMOKE_TIMEOUT", "10"))


def main() -> int:
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        print("gateway_black_box_smoke: API_KEY not set", file=sys.stderr)
        return 1

    req = urllib.request.Request(
        "http://localhost:8085/api/ssh/sessions",
        headers={"X-API-Key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                print(f"gateway_black_box_smoke: unexpected HTTP {resp.status}", file=sys.stderr)
                return 1
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"gateway_black_box_smoke: HTTP {exc.code}: {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"gateway_black_box_smoke: {exc}", file=sys.stderr)
        return 1

    if not isinstance(body, dict) or "sessions" not in body or "count" not in body:
        print(f"gateway_black_box_smoke: unexpected response shape: {body!r}", file=sys.stderr)
        return 1
    if not isinstance(body["sessions"], list) or body["count"] != len(body["sessions"]):
        print(f"gateway_black_box_smoke: count/sessions mismatch: {body!r}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
