"""Bounded in-process MCP transport for candidate site assets.

MCP tool arguments cannot carry arbitrary server-local attachments. This
protocol receives explicitly supplied base64 chunks, and delegates the final
write to the same guarded workspace-assets implementation as REST.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.workspace.assets import (
    MAX_ASSET_BYTES,
    SHA256_RE,
    AssetConflict,
    AssetError,
    _candidate_path,
    inspect_asset,
    publish_asset,
)
from app.workspace.registry import WorkspaceRegistry

CHUNK_BYTES = 64 * 1024
MAX_SESSIONS = 4
UPLOAD_TTL_SECONDS = 15 * 60
_MAX_BASE64_CHARS = 4 * ((CHUNK_BYTES + 2) // 3)


@dataclass
class _Upload:
    project_id: str
    relative_path: str
    sha256: str
    total_bytes: int
    previous_sha256: str | None
    create_parents: bool
    updated: float
    content: bytearray = field(default_factory=bytearray)


class AssetTransferStore:
    """Small bounded, restart-discardable staging area; never writes to Git."""

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._uploads: dict[str, _Upload] = {}

    def _expire(self) -> None:
        now = time.monotonic()
        for upload_id, entry in list(self._uploads.items()):
            if now - entry.updated >= UPLOAD_TTL_SECONDS:
                self._uploads.pop(upload_id)

    @staticmethod
    def _require_writeable() -> None:
        from app.config import settings

        if settings.workspace_readonly:
            raise AssetError("Workspace is in read-only mode")

    def _get(self, upload_id: str, project_id: str, relative_path: str) -> _Upload:
        entry = self._uploads.get(upload_id)
        if entry is None:
            raise AssetError("Upload session not found or expired; begin a new upload")
        if entry.project_id != project_id or entry.relative_path != relative_path:
            raise AssetError("Upload session does not match project and path")
        return entry

    @staticmethod
    def _progress(upload_id: str, entry: _Upload) -> dict[str, Any]:
        return {
            "upload_id": upload_id,
            "project_id": entry.project_id,
            "path": entry.relative_path,
            "received_bytes": len(entry.content),
            "total_bytes": entry.total_bytes,
            "next_offset": len(entry.content),
            "sha256": entry.sha256,
            "chunk_bytes_max": CHUNK_BYTES,
            "ttl_seconds": UPLOAD_TTL_SECONDS,
        }

    def begin(
        self,
        registry: WorkspaceRegistry,
        project_id: str,
        relative_path: str,
        sha256: str,
        total_bytes: int,
        *,
        previous_sha256: str | None,
        create_parents: bool,
    ) -> dict[str, Any]:
        self._require_writeable()
        if not SHA256_RE.fullmatch(sha256):
            raise AssetError("SHA-256 must be 64 lowercase hexadecimal characters")
        if previous_sha256 is not None and not SHA256_RE.fullmatch(previous_sha256):
            raise AssetError("Previous SHA-256 must be 64 lowercase hexadecimal characters")
        if type(total_bytes) is not int or not 1 <= total_bytes <= MAX_ASSET_BYTES:
            raise AssetError(f"Asset must be 1..{MAX_ASSET_BYTES} bytes")
        _candidate_path(registry, project_id, relative_path, write=True)
        with self._guard:
            self._expire()
            if len(self._uploads) >= MAX_SESSIONS:
                raise AssetError("Too many in-progress uploads; finish or abort an upload")
            upload_id = uuid.uuid4().hex
            entry = _Upload(
                project_id=project_id, relative_path=relative_path,
                sha256=sha256, total_bytes=total_bytes,
                previous_sha256=previous_sha256, create_parents=create_parents,
                updated=time.monotonic(),
            )
            self._uploads[upload_id] = entry
            return {"action": "begin", **self._progress(upload_id, entry)}

    def status(self, upload_id: str, project_id: str, relative_path: str) -> dict[str, Any]:
        with self._guard:
            self._expire()
            entry = self._get(upload_id, project_id, relative_path)
            return {"action": "status", **self._progress(upload_id, entry)}

    def chunk(
        self,
        upload_id: str,
        project_id: str,
        relative_path: str,
        offset: int,
        chunk_b64: str,
        chunk_sha256: str,
    ) -> dict[str, Any]:
        self._require_writeable()
        if type(offset) is not int or offset < 0:
            raise AssetError("offset must be a nonnegative integer")
        if not isinstance(chunk_b64, str) or not 1 <= len(chunk_b64) <= _MAX_BASE64_CHARS:
            raise AssetError("Base64 chunk exceeds the 64 KiB decoded maximum")
        if not SHA256_RE.fullmatch(chunk_sha256):
            raise AssetError("Chunk SHA-256 must contain 64 lowercase hex characters")
        try:
            chunk = base64.b64decode(chunk_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AssetError("Invalid RFC4648 base64 chunk") from exc
        if not chunk or len(chunk) > CHUNK_BYTES:
            raise AssetError("Chunk must contain 1..65536 decoded bytes")
        if not hmac.compare_digest(hashlib.sha256(chunk).hexdigest(), chunk_sha256):
            raise AssetError("Chunk SHA-256 mismatch; no bytes accepted")

        with self._guard:
            self._expire()
            entry = self._get(upload_id, project_id, relative_path)
            current = len(entry.content)
            if offset < current and offset + len(chunk) <= current:
                original = entry.content[offset : offset + len(chunk)]
                if hmac.compare_digest(original, chunk):
                    entry.updated = time.monotonic()
                    return {"action": "chunk", "replayed": True, **self._progress(upload_id, entry)}
            if offset != current:
                raise AssetConflict(f"Unexpected offset; next_offset={current}")
            if current + len(chunk) > entry.total_bytes:
                raise AssetError("Chunk exceeds the declared file size")
            entry.content.extend(chunk)
            entry.updated = time.monotonic()
            return {"action": "chunk", "replayed": False, **self._progress(upload_id, entry)}

    def finish(
        self,
        registry: WorkspaceRegistry,
        upload_id: str,
        project_id: str,
        relative_path: str,
    ) -> dict[str, Any]:
        self._require_writeable()
        with self._guard:
            self._expire()
            entry = self._get(upload_id, project_id, relative_path)
            if len(entry.content) != entry.total_bytes:
                raise AssetConflict(f"Upload incomplete; next_offset={len(entry.content)}")
            data = bytes(entry.content)
            if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), entry.sha256):
                raise AssetError("Complete-file SHA-256 mismatch; no file was written")
            result = publish_asset(
                registry, project_id, relative_path, data, entry.sha256,
                previous_sha256=entry.previous_sha256,
                create_parents=entry.create_parents,
            )
            del self._uploads[upload_id]
            return {"action": "finish", "uploaded": True, **result}

    def abort(self, upload_id: str, project_id: str, relative_path: str) -> dict[str, Any]:
        with self._guard:
            self._expire()
            self._get(upload_id, project_id, relative_path)
            del self._uploads[upload_id]
            return {"action": "abort", "aborted": True, "upload_id": upload_id}

    @staticmethod
    def inspect(registry: WorkspaceRegistry, project_id: str, relative_path: str) -> dict[str, Any]:
        return {"action": "inspect", **inspect_asset(registry, project_id, relative_path)}

    @staticmethod
    def read(
        registry: WorkspaceRegistry,
        project_id: str,
        relative_path: str,
        offset: int,
        expected_sha256: str,
    ) -> dict[str, Any]:
        if type(offset) is not int or offset < 0:
            raise AssetError("offset must be a nonnegative integer")
        if not SHA256_RE.fullmatch(expected_sha256):
            raise AssetError("Expected SHA-256 must be 64 lowercase hex characters")
        meta = inspect_asset(registry, project_id, relative_path)
        if meta["sha256"] != expected_sha256:
            raise AssetConflict("Asset SHA-256 changed: refresh metadata")
        if offset > meta["size"]:
            raise AssetError("offset exceeds file size")
        path = _candidate_path(registry, project_id, relative_path, write=False)
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(CHUNK_BYTES)
        # Do not return a chunk from a replacement file after the metadata read.
        if inspect_asset(registry, project_id, relative_path)["sha256"] != expected_sha256:
            raise AssetConflict("Asset changed during read")
        return {
            "action": "read", "project_id": project_id, "path": relative_path,
            "offset": offset, "next_offset": offset + len(chunk),
            "eof": offset + len(chunk) == meta["size"],
            "size": meta["size"], "sha256": expected_sha256,
            "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
            "chunk_b64": base64.b64encode(chunk).decode("ascii"),
        }
