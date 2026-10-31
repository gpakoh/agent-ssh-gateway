"""Read-only runtime connectivity/auth smoke inside the deployed OAuth container.

No operator token leaves the container. This is a narrow Gateway Media
attachment check, NOT an image-conversion or candidate-CAS qualification.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

MEDIA_BASE = "http://media-processing:8091"
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class MediaSmokeError(Exception):
    pass


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise MediaSmokeError("unexpected Media API redirect")


def smoke(env: dict[str, str]) -> None:
    if env.get("MEDIA_API_BASE_URL") != MEDIA_BASE:
        raise MediaSmokeError("Media API endpoint mismatch")
    token = env.get("SITE_MEDIA_WORKER_TOKEN", "")
    if not 32 <= len(token) <= 512 or any(not 33 <= ord(c) <= 126 for c in token):
        raise MediaSmokeError("invalid Media API client credential")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirect())

    def get(path: str, credential: str | None = None) -> tuple[int, bytes]:
        headers = {} if credential is None else {"X-Media-Token": credential}
        req = urllib.request.Request(MEDIA_BASE + path, headers=headers)
        try:
            with opener.open(req, timeout=5) as response:
                body = response.read(16385)
                if len(body) > 16384:
                    raise MediaSmokeError("Media response too large")
                return response.status, body
        except urllib.error.HTTPError as exc:
            return exc.code, b""
        except (OSError, TimeoutError) as exc:
            raise MediaSmokeError("Media API inaccessible") from exc

    status, body = get("/healthz")
    if status != 200 or body != b"ok":
        raise MediaSmokeError("Media liveness failed")
    status, body = get("/ready")
    try:
        readiness = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MediaSmokeError("Media readiness invalid") from exc
    if status != 200 or not isinstance(readiness, dict) or readiness.get("status") != "ready":
        raise MediaSmokeError("Media not ready")
    if not isinstance(readiness.get("build"), str) or not SHA40.fullmatch(readiness["build"]):
        raise MediaSmokeError("Media release SHA missing")
    status, body = get("/v1/capabilities", token)
    try:
        capabilities = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MediaSmokeError("Media capabilities invalid") from exc
    if status != 200 or not isinstance(capabilities, dict) or capabilities.get("build") != readiness["build"]:
        raise MediaSmokeError("Media build mismatch")
    operations = capabilities.get("operations")
    if not isinstance(operations, list) or not any(
        isinstance(item, dict) and item.get("name") == "raster.convert"
        and item.get("status") == "available" for item in operations
    ):
        raise MediaSmokeError("Media conversion unavailable")

    # This value is longer than the API's maximum supported token length.
    invalid_status, _ = get("/v1/capabilities", "z" * 513)
    if invalid_status != 403:
        raise MediaSmokeError("Media rejected-token invariant failed")


def main() -> int:
    try:
        smoke(dict(os.environ))
    except (MediaSmokeError, ValueError):
        print("Gateway Media runtime smoke: NO-GO", file=sys.stderr)
        return 1
    print("Gateway Media runtime smoke: GO (private auth/readiness)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
