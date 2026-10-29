"""Candidate-scoped binary assets for website development.

No shell, URL fetch, production write or MCP tool proliferation. The REST
transport preserves original bytes and uses the existing workspace policy.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import stat
from pathlib import Path
from typing import Any

from app.workspace.edit import (
    WriteError,
    _atomic_write,
    _create_parent_directories,
    _symlink_safe_preflight,
)
from app.workspace.policy import WorkspacePolicyError
from app.workspace.registry import WorkspaceRegistry

MAX_ASSET_BYTES = 8 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ASSET_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


class AssetError(WriteError):
    """Invalid static asset, checksum or explicit replacement lease."""


class AssetConflict(AssetError):
    """Existing content differs from the caller's revision lease."""


def _candidate_path(
    registry: WorkspaceRegistry, project_id: str, relative_path: str, *, write: bool
) -> Path:
    # Only managed candidate clones may receive assets; not source checkout or
    # a production asset directory. The workspace registry remains authoritative.
    if not project_id.startswith("candidate-"):
        raise AssetError("Registered candidate workspace is required for site assets")
    parts = Path(relative_path).parts
    if (
        len(parts) < 3
        or parts[0] not in {"public", "src"}
        or (parts[0] == "src" and parts[1] != "assets")
        or (parts[0] == "public" and parts[1] not in {"visuals", "fonts", "images", "icons"})
        or Path(relative_path).suffix.lower() not in ASSET_TYPES
    ):
        raise AssetError("Only approved binary paths in public/{visuals,fonts,images,icons} or src/assets are allowed")
    root = registry._policy._resolve_project_root(project_id)
    path = (
        registry._policy.validate_write(project_id, relative_path)
        if write else registry._policy.validate_read(project_id, relative_path)
    )
    _symlink_safe_preflight(path, root)
    return path


def _asset_kind(data: bytes, suffix: str) -> bool:
    if suffix == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24
    if suffix in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    if suffix == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if suffix == ".avif":
        return len(data) >= 32 and data[4:8] == b"ftyp" and (
            b"avif" in data[8:32] or b"avis" in data[8:32]
        )
    if suffix == ".ico":
        return len(data) >= 8 and data[:4] == b"\x00\x00\x01\x00"
    if suffix == ".woff2":
        return len(data) >= 48 and data.startswith(b"wOF2")
    if suffix == ".ttf":
        return len(data) >= 12 and data[:4] in (b"\x00\x01\x00\x00", b"true")
    return False


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(128 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def inspect_asset(
    registry: WorkspaceRegistry, project_id: str, relative_path: str
) -> dict[str, Any]:
    path = _candidate_path(registry, project_id, relative_path, write=False)
    if not path.is_file() or path.is_symlink():
        raise WorkspacePolicyError("Asset not found")
    size = path.stat().st_size
    if size > MAX_ASSET_BYTES:
        raise AssetError(f"Asset exceeds maximum of {MAX_ASSET_BYTES} bytes")
    result: dict[str, Any] = {
        "project_id": project_id,
        "path": relative_path,
        "size": size,
        "sha256": _file_sha256(path),
        "media_type": ASSET_TYPES[path.suffix.lower()],
    }
    # PNG intrinsic dimensions require only the fixed IHDR prefix. Do not
    # invoke a decoder, trust external metadata, or load the entire asset.
    if path.suffix.lower() == ".png":
        with path.open("rb") as stream:
            header = stream.read(24)
        if header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR":
            width = int.from_bytes(header[16:20], "big")
            height = int.from_bytes(header[20:24], "big")
            if width and height:
                result["width"] = width
                result["height"] = height
    return result


def publish_asset(
    registry: WorkspaceRegistry,
    project_id: str,
    relative_path: str,
    data: bytes,
    expected_sha256: str,
    *,
    previous_sha256: str | None = None,
    create_parents: bool = False,
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise AssetError("A lowercase 64-character SHA-256 is required")
    if previous_sha256 is not None and not SHA256_RE.fullmatch(previous_sha256):
        raise AssetError("Previous SHA-256 must contain 64 lowercase hex characters")
    if not data:
        raise AssetError("Empty asset is not allowed")
    if len(data) > MAX_ASSET_BYTES:
        raise AssetError(f"Asset exceeds maximum of {MAX_ASSET_BYTES} bytes")
    digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(expected_sha256, digest):
        raise AssetError("Asset SHA-256 mismatch: no file was changed")

    path = _candidate_path(registry, project_id, relative_path, write=True)
    if not _asset_kind(data, path.suffix.lower()):
        raise AssetError("Invalid asset format/signature: no file was changed")
    if path.exists() and not path.is_file():
        raise AssetError("Asset target is not a regular file")
    root = registry._policy._resolve_project_root(project_id)
    if not path.parent.exists():
        if not create_parents:
            raise AssetError("Parent directory does not exist")
        _create_parent_directories(path, root)
    _symlink_safe_preflight(path, root)
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise AssetError("Asset target is not a regular file")

    # Directory flock serializes uploads of sibling assets across API worker
    # processes without leaving a lock file in the published repository.
    import fcntl
    import os

    lock_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _symlink_safe_preflight(path, root)
        if path.exists() and not path.is_file():
            raise AssetError("Asset target is not a regular file")
        old_sha = _file_sha256(path) if path.exists() else None
        if previous_sha256 is None:
            if old_sha is not None:
                raise AssetConflict("Target exists: provide its exact SHA-256 to replace")
        elif old_sha != previous_sha256:
            raise AssetConflict("Existing asset SHA-256 has changed: reload metadata")

        _atomic_write(path, data)
        final = inspect_asset(registry, project_id, relative_path)
        if final["sha256"] != digest:
            raise AssetError("Written asset verification failed")
        return {**final, "previous_sha256": old_sha, "verified": True}
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
