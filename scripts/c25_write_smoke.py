#!/usr/bin/env python3
"""Safe smoke test for workspace write/edit/patch with safe=true.

Usage:
    API_KEY=... python scripts/c25_write_smoke.py [--base URL]
    python scripts/c25_write_smoke.py [--base URL] --api-key KEY

Requires WORKSPACE_READONLY=false on the gateway.
Cleans up _c25_write_smoke.txt after test.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

# Add parent dir to path for SDK import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdk.ssh_gateway import SSHGatewayClient


def compute_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def run_smoke(base_url: str, api_key: str) -> bool:
    pid = "web-ssh-gateway"
    path = "_c25_write_smoke.txt"
    all_pass = True

    def check(label: str, condition: bool, detail: str = ""):
        nonlocal all_pass
        status = "PASS" if condition else "FAIL"
        if not condition:
            all_pass = False
        suffix = f" — {detail}" if detail else ""
        print(f"  [{status}] {label}{suffix}")

    print("=== C2.5 Write Smoke ===\n")

    # 1. Write safe=true
    print("1. workspace_write safe=True")
    try:
        r = SSHGatewayClient.workspace_write(
            pid, path, "hello smoke", safe=True,
            base_url=base_url, api_key=api_key,
        )
        check("returns receipt", "receipt" in r)
        receipt = r.get("receipt", {})
        check("receipt_id present", bool(receipt.get("receipt_id")))
        check("verified=True", receipt.get("verified") is True)
        check("file_exists_before=False", receipt.get("file_exists_before") is False)
        check("size=11", r.get("size") == 11)
        check("after_hash matches", receipt.get("after_hash") == compute_hash("hello smoke"))
    except Exception as e:
        check(f"write raised {type(e).__name__}: {e}", False)

    # 2. Verify hash
    print("\n2. workspace_verify")
    try:
        r = SSHGatewayClient.workspace_verify(
            pid, path, compute_hash("hello smoke"),
            base_url=base_url, api_key=api_key,
        )
        check("matches=True", r.get("matches") is True)
        check("file_exists=True", r.get("file_exists") is True)
    except Exception as e:
        check(f"verify raised {type(e).__name__}: {e}", False)

    # 3. Edit safe=true
    print("\n3. workspace_edit safe=True")
    try:
        r = SSHGatewayClient.workspace_edit(
            pid, path, "smoke", "done",
            safe=True, base_url=base_url, api_key=api_key,
        )
        check("replaced=True", r.get("replaced") is True)
        receipt = r.get("receipt", {})
        check("receipt_id present", bool(receipt.get("receipt_id")))
        check("verified=True", receipt.get("verified") is True)
        check("file_exists_before=True", receipt.get("file_exists_before") is True)
    except Exception as e:
        check(f"edit raised {type(e).__name__}: {e}", False)

    # 4. Verify after edit
    print("\n4. workspace_verify after edit")
    try:
        r = SSHGatewayClient.workspace_verify(
            pid, path, compute_hash("hello done"),
            base_url=base_url, api_key=api_key,
        )
        check("matches=True", r.get("matches") is True)
    except Exception as e:
        check(f"verify raised {type(e).__name__}: {e}", False)

    # 5. Patch safe=true
    print("\n5. workspace_patch safe=True")
    patch = (
        "--- a/_c25_write_smoke.txt\n"
        "+++ b/_c25_write_smoke.txt\n"
        "@@ -1 +1 @@\n"
        "-hello done\n"
        "+patched safe\n"
    )
    try:
        r = SSHGatewayClient.workspace_patch(
            pid, path, patch, safe=True,
            base_url=base_url, api_key=api_key,
        )
        check("applied=True", r.get("applied") is True)
        receipt = r.get("receipt", {})
        check("receipt_id present", bool(receipt.get("receipt_id")))
        check("verified=True", receipt.get("verified") is True)
    except Exception as e:
        check(f"patch raised {type(e).__name__}: {e}", False)

    # 6. Final verify
    print("\n6. workspace_verify after patch")
    try:
        r = SSHGatewayClient.workspace_verify(
            pid, path, compute_hash("patched safe"),
            base_url=base_url, api_key=api_key,
        )
        check("matches=True", r.get("matches") is True)
    except Exception as e:
        check(f"verify raised {type(e).__name__}: {e}", False)

    # 7. Cleanup
    print("\n7. Cleanup")
    smoke_file = Path(__file__).resolve().parent.parent / path
    if smoke_file.exists():
        smoke_file.unlink()
        check("file deleted", True)
    else:
        check("file not found (expected if write failed)", True)

    print(f"\n{'='*30}")
    print(f"RESULT: {'ALL PASS' if all_pass else 'SOME FAILURES'}")
    return all_pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="C2.5 write smoke test")
    parser.add_argument("--base", default="http://localhost:8085")
    parser.add_argument("--api-key", default=os.environ.get("API_KEY"))
    args = parser.parse_args()
    if not args.api_key:
        parser.error("API key required: pass --api-key or set API_KEY")

    ok = run_smoke(args.base, args.api_key)
    sys.exit(0 if ok else 1)
