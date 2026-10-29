"""Policy-fenced bridge from a candidate asset to an isolated media codec worker.

This module never imports or executes an image decoder. All bytes cross an
explicit, token-authenticated internal HTTP boundary with no caller URL.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import urllib.error
import urllib.request
from typing import Any

from app.workspace.assets import (
    MAX_ASSET_BYTES,
    SHA256_RE,
    AssetConflict,
    AssetError,
    _asset_kind,
    _candidate_path,
    inspect_asset,
    publish_asset,
)
from app.workspace.registry import WorkspaceRegistry

_WORKER_URL = "http://site-media-worker:8091/v1/convert"


class MediaUnavailable(AssetError):
    """Isolated worker absent or unable to verify a trustworthy result."""


def convert_candidate_image(
    registry: WorkspaceRegistry, project_id: str, source_path: str, target_path: str,
    source_sha256: str, *, codec: str = "avif", quality: int = 95,
    max_width: int = 0, min_psnr: float = 36.0,
    previous_sha256: str | None = None,
) -> dict[str, Any]:
    from app.config import settings

    if settings.workspace_readonly:
        raise AssetError("Workspace is read-only")
    if not SHA256_RE.fullmatch(source_sha256):
        raise AssetError("source_sha256 must be a lowercase 64-character SHA-256")
    if source_path == target_path:
        raise AssetError("Original source cannot be overwritten")
    if codec not in {"webp", "avif", "png"} or not target_path.lower().endswith("." + codec):
        raise AssetError("Target extension and codec must match")
    if type(quality) is not int or not 1 <= quality <= 100:
        raise AssetError("quality must be 1..100")
    if type(max_width) is not int or max_width != 0 and not 320 <= max_width <= 4096:
        raise AssetError("max_width must be 0 or 320..4096")
    if not isinstance(min_psnr, (int, float)) or not math.isfinite(min_psnr) or not 0 <= min_psnr <= 99:
        raise AssetError("min_psnr must be finite and in 0..99")
    token = os.environ.get("SITE_MEDIA_WORKER_TOKEN", "")
    if len(token) < 32:
        raise MediaUnavailable("Isolated site media worker not configured")
    source = _candidate_path(registry, project_id, source_path, write=False)
    _candidate_path(registry, project_id, target_path, write=True)
    metadata = inspect_asset(registry, project_id, source_path)
    if metadata["sha256"] != source_sha256:
        raise AssetConflict("Source SHA-256 has changed")
    if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".avif"}:
        raise AssetError("Source must be a supported image")
    data = source.read_bytes()
    if not data or len(data) > MAX_ASSET_BYTES or hashlib.sha256(data).hexdigest() != source_sha256:
        raise AssetConflict("Source changed during read")
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Media-Token": token,
        "X-Media-Source-Sha256": source_sha256,
        "X-Media-Codec": codec,
        "X-Media-Quality": str(quality),
        "X-Media-Max-Width": str(max_width),
        "X-Media-Min-PSNR": str(min_psnr),
    }
    request = urllib.request.Request(_WORKER_URL, data=data, headers=headers, method="POST")
    # Proxies from the host environment must never intercept private source bytes.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=90) as response:
            if response.status != 200:
                raise MediaUnavailable("Media worker did not accept the request")
            if response.headers.get("Content-Type", "").split(";", 1)[0] != {
                "png": "image/png", "webp": "image/webp", "avif": "image/avif"
            }[codec]:
                raise MediaUnavailable("Media worker returned an unexpected MIME type")
            out = response.read(MAX_ASSET_BYTES + 1)
            received = dict(response.headers)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise MediaUnavailable("Isolated media worker unavailable or failed") from exc
    if not out or len(out) > MAX_ASSET_BYTES or not _asset_kind(out, "." + codec):
        raise MediaUnavailable("Worker result is empty, oversized or has invalid magic")
    digest = hashlib.sha256(out).hexdigest()
    if not hmac.compare_digest(digest, received.get("X-Media-Output-Sha256", "")):
        raise MediaUnavailable("Worker output checksum mismatch")
    if not hmac.compare_digest(source_sha256, received.get("X-Media-Source-Sha256", "")):
        raise MediaUnavailable("Worker source checksum mismatch")
    try:
        width = int(received["X-Media-Width"])
        height = int(received["X-Media-Height"])
        psnr = float(received["X-Media-PSNR"])
    except (KeyError, ValueError) as exc:
        raise MediaUnavailable("Worker omitted conversion evidence") from exc
    if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 16_000_000:
        raise MediaUnavailable("Worker reported invalid output dimensions")
    if not math.isfinite(psnr) or psnr < min_psnr or psnr > 99:
        raise MediaUnavailable("Worker output failed the quality floor")
    if inspect_asset(registry, project_id, source_path)["sha256"] != source_sha256:
        raise AssetConflict("Source was replaced during conversion")
    saved = publish_asset(
        registry, project_id, target_path, out, digest,
        previous_sha256=previous_sha256,
    )
    return {
        "action": "convert", "source": source_path, "source_sha256": source_sha256,
        "output": saved, "width": width, "height": height, "codec": codec,
        "quality": quality, "min_psnr": min_psnr, "psnr": psnr,
        "max_width": max_width, "verified": True,
    }
