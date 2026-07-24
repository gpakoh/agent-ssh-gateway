#!/usr/bin/env python3
"""Access-control smoke test for v0.1.45a0 operator loop.

Validates the full cycle: health → set deny → verify recent → clear → verify clear.
No real SSH network required.

Environment:
  GATEWAY_URL          (default http://localhost:8085)
  GATEWAY_API_KEY      (required)
  TEST_ACTOR_FP        (default smoke-test-fp-000000000000)
  TEST_SOURCE_IP       (default 10.99.99.99)
"""

from __future__ import annotations

import os
import sys

import httpx

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8085").rstrip("/")
API_KEY = os.getenv("GATEWAY_API_KEY", "")
ACTOR_FP = os.getenv("TEST_ACTOR_FP", "smoke-test-fp-000000000000")
SOURCE_IP = os.getenv("TEST_SOURCE_IP", "10.99.99.99")

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "✅" if ok else "❌"
    suffix = f" — {detail}" if detail else ""
    print(f"  {status} {label}{suffix}")
    if ok:
        PASS += 1
    else:
        FAIL += 1


def main() -> int:
    if not API_KEY:
        print("❌ GATEWAY_API_KEY not set")
        return 1

    headers = {"X-API-Key": API_KEY}
    with httpx.Client(timeout=10) as c:

        # 1. Health check
        r = c.get(f"{GATEWAY_URL}/health")
        health = r.json()
        check("health", r.status_code == 200, f"version={health.get('version')} status={health.get('status')}")
        check("health ready", health.get("ready") is True)

        # 2. Set deny
        r = c.post(
            f"{GATEWAY_URL}/api/admin/access-control/decision",
            json={
                "actor_fingerprint": ACTOR_FP,
                "source_ip": SOURCE_IP,
                "decision": "deny",
                "reason": "smoke test",
            },
            headers=headers,
        )
        body = r.json()
        check("POST deny", r.status_code == 200, f"decision={body.get('decision')}")
        check("deny normalized", body.get("decision") == "deny")

        # 3. Verify recent shows denied
        r = c.get(
            f"{GATEWAY_URL}/api/admin/access-control/recent",
            params={"limit": 10, "decision": "denied"},
            headers=headers,
        )
        recent = r.json()
        check("GET recent", r.status_code == 200, f"total={recent.get('total')}")
        found = any(
            d["actor_fingerprint"] == ACTOR_FP and d["decision"] == "denied"
            for d in recent.get("decisions", [])
        )
        check("recent contains denied actor", found)
        if recent.get("decisions"):
            ttl = recent["decisions"][0].get("ttl_seconds_remaining", -1)
            check("ttl_seconds_remaining present", isinstance(ttl, float) and ttl > 0, f"ttl={ttl}")

        # 4. Clear decision
        r = c.post(
            f"{GATEWAY_URL}/api/admin/access-control/clear",
            json={
                "actor_fingerprint": ACTOR_FP,
                "source_ip": SOURCE_IP,
                "reason": "smoke test cleanup",
            },
            headers=headers,
        )
        clear_body = r.json()
        check("POST clear", r.status_code == 200, f"cleared={clear_body.get('cleared')}")

        # 5. Verify clear — no more denied in recent
        r = c.get(
            f"{GATEWAY_URL}/api/admin/access-control/recent",
            params={"limit": 10, "decision": "denied"},
            headers=headers,
        )
        recent_after = r.json()
        still_denied = any(
            d["actor_fingerprint"] == ACTOR_FP
            for d in recent_after.get("decisions", [])
        )
        check("clear removes from recent", not still_denied)

    print(f"\n{'='*40}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
