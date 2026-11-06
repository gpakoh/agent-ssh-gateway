"""Bounded, token-isolated Gitea Actions ZIP transport (no artifact publication).

The higher-level caller must prove the live PR, run, attempt and artifact ID
before invoking this transport, and must revoke/delete its private temporary
file when that lease changes. This module never follows a redirect with the
Gitea API client's credentials or extracts untrusted archive members.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from collections.abc import Collection
from pathlib import PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import urljoin, urlsplit

import httpx

MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_ZIP_ENTRIES = 256
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_EXPANSION_RATIO = 200
_VERSION_RE = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)(?:\+[^\s]+)?$")
_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class ArtifactTransportError(ValueError):
    """Fixed, non-sensitive failure; never echo provider URLs or headers."""


def require_patched_gitea_version(payload: Any) -> None:
    """Reject signed artifact downloads from vulnerable or unproven Gitea.

    GHSA-hg5r-vq93-9fv6 affects Gitea Actions V4 signed URLs through 1.26.1.
    Do not infer safety from a missing or decorated/prerelease version.
    """
    if not isinstance(payload, dict):
        raise ArtifactTransportError("Gitea version evidence is unavailable")
    raw = payload.get("version")
    match = _VERSION_RE.fullmatch(raw) if isinstance(raw, str) else None
    if match is None:
        raise ArtifactTransportError("Gitea version evidence is unavailable")
    version = tuple(int(group) for group in match.groups())
    if version < (1, 26, 2):
        raise ArtifactTransportError("Gitea artifact transport requires a patched server")


def _origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ArtifactTransportError("Untrusted artifact redirect origin") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or any(ch in url for ch in ("\\", "\r", "\n", "\x00"))
    ):
        raise ArtifactTransportError("Untrusted artifact redirect origin")
    host = parts.hostname.lower()
    return f"https://{host}:{port}" if port not in (None, 443) else f"https://{host}"


def _validate_allowed_origins(origins: Collection[str]) -> frozenset[str]:
    safe: set[str] = set()
    for value in origins:
        if not isinstance(value, str) or len(value) > 256:
            raise ArtifactTransportError("Artifact redirect allowlist is invalid")
        try:
            parts = urlsplit(value)
        except ValueError:
            raise ArtifactTransportError("Artifact redirect allowlist is invalid") from None
        if (
            parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or _origin(value) != value.rstrip("/")
        ):
            raise ArtifactTransportError("Artifact redirect allowlist is invalid")
        safe.add(_origin(value))
    return frozenset(safe)


def _redirect_target(location: str | None, base: str, origins: frozenset[str]) -> str:
    if (
        not isinstance(location, str)
        or not location
        or len(location) > 4096
        or any(ch in location for ch in ("\\", "\r", "\n", "\x00"))
    ):
        raise ArtifactTransportError("Gitea artifact redirect is invalid")
    try:
        target = urljoin(base, location)
        parts = urlsplit(target)
    except ValueError:
        raise ArtifactTransportError("Gitea artifact redirect is invalid") from None
    if (
        parts.fragment
        or not parts.path.startswith("/")
        or _origin(target) not in origins
    ):
        raise ArtifactTransportError("Gitea artifact redirect origin is not approved")
    return target


def validate_zip_archive(file: BinaryIO) -> dict[str, int]:
    """Inspect the bounded ZIP directory without extraction or decompression."""
    try:
        file.flush()
        file.seek(0)
        with zipfile.ZipFile(file, "r") as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise ArtifactTransportError("Artifact ZIP has too many members")
            total = 0
            seen: set[str] = set()
            for info in entries:
                raw_name = info.filename
                normalized_name = raw_name.rstrip("/").casefold()
                member_mode = stat.S_IFMT(info.external_attr >> 16)
                parts = PurePosixPath(raw_name).parts
                if (
                    not raw_name
                    or len(raw_name) > 512
                    or raw_name.startswith("/")
                    or "\\" in raw_name
                    or "\x00" in raw_name
                    or any(part in ("..", ".", "") for part in raw_name.rstrip("/").split("/"))
                    or any(ord(ch) < 32 for ch in raw_name)
                    or not parts
                    or normalized_name in seen
                    or info.flag_bits & 0x1
                    or member_mode not in (0, stat.S_IFDIR, stat.S_IFREG)
                    or (member_mode == stat.S_IFDIR and not info.is_dir())
                ):
                    raise ArtifactTransportError("Artifact ZIP contains an unsafe member")
                seen.add(normalized_name)
                if info.file_size > MAX_UNCOMPRESSED_BYTES:
                    raise ArtifactTransportError("Artifact ZIP member exceeds the size cap")
                total += info.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise ArtifactTransportError("Artifact ZIP exceeds the expansion cap")
                if info.file_size > MAX_EXPANSION_RATIO * max(1, info.compress_size):
                    raise ArtifactTransportError("Artifact ZIP compression ratio is unsafe")
            return {"entries": len(entries), "uncompressed_bytes": total}
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError):
        raise ArtifactTransportError("Artifact is not a valid bounded ZIP") from None


async def stream_action_artifact_zip(
    gitea_client: httpx.AsyncClient,
    owner: str,
    repo: str,
    artifact_id: int,
    output: BinaryIO,
    *,
    redirect_origins: Collection[str],
    max_bytes: int = MAX_ZIP_BYTES,
    blob_client: httpx.AsyncClient | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Stream one exact ID into a caller-owned private temporary file.

    The initial API request may authenticate to Gitea. A 302 is handled
    manually using a *different*, credential-free HTTP client. There are
    no further redirects. Any transport or validation error truncates output;
    upstream must also unlink its temporary inode on error/cancellation.
    """
    for segment in (owner, repo):
        if not isinstance(segment, str) or not _REPO_SEGMENT_RE.fullmatch(segment) or ".." in segment:
            raise ArtifactTransportError("Invalid artifact repository identity")
    if type(artifact_id) is not int or artifact_id < 1:
        raise ArtifactTransportError("Invalid artifact ID")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ZIP_BYTES:
        raise ArtifactTransportError("Invalid artifact download byte cap")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise ArtifactTransportError("Invalid expected artifact checksum")
    if not output.seekable() or output.tell() != 0:
        raise ArtifactTransportError("Artifact destination must be empty and seekable")

    allowed = _validate_allowed_origins(redirect_origins)
    api_path = f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip"
    digest = hashlib.sha256()
    size = 0

    async def prove_patched_server() -> None:
        # Fail closed before any signed URL is issued. Bound provider JSON.
        async with gitea_client.stream("GET", "/version", follow_redirects=False) as version:
            if version.status_code != 200:
                raise ArtifactTransportError("Gitea version evidence is unavailable")
            data = bytearray()
            async for part in version.aiter_bytes(chunk_size=1024):
                if len(data) + len(part) > 2048:
                    raise ArtifactTransportError("Gitea version response exceeds the byte cap")
                data.extend(part)
        try:
            parsed = json.loads(data)
        except (ValueError, UnicodeError):
            raise ArtifactTransportError("Gitea version evidence is unavailable") from None
        require_patched_gitea_version(parsed)

    async def write_chunks(response: httpx.Response) -> None:
        nonlocal size
        if response.status_code != 200:
            raise ArtifactTransportError("Artifact download did not return ZIP data")
        async for chunk in response.aiter_bytes(chunk_size=8192):
            size += len(chunk)
            if size > max_bytes:
                raise ArtifactTransportError("Artifact ZIP exceeds the byte cap")
            output.write(chunk)
            digest.update(chunk)

    try:
        await prove_patched_server()
        async with gitea_client.stream("GET", api_path, follow_redirects=False) as api:
            if api.status_code in (301, 302, 303, 307, 308):
                target = _redirect_target(
                    api.headers.get("location"), str(api.request.url), allowed
                )
                if blob_client is None:
                    async with httpx.AsyncClient(
                        follow_redirects=False,
                        trust_env=False,
                        headers={"Accept": "application/zip"},
                        timeout=httpx.Timeout(30.0, connect=10.0),
                    ) as clean_client:
                        async with clean_client.stream("GET", target, follow_redirects=False) as blob:
                            await write_chunks(blob)
                else:
                    # Tests can inject a credential-free transport. Production
                    # always constructs a separate client with no API token.
                    async with blob_client.stream("GET", target, follow_redirects=False) as blob:
                        await write_chunks(blob)
            else:
                await write_chunks(api)
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise ArtifactTransportError("Artifact ZIP checksum does not match")
        archive = validate_zip_archive(output)
        return {"size_bytes": size, "sha256": digest.hexdigest(), **archive}
    except (httpx.RequestError, httpx.InvalidURL):
        # httpx may parse a malformed Location even with redirects disabled.
        # Its exception embeds signed URLs; never leak that exception text.
        output.seek(0)
        output.truncate(0)
        raise ArtifactTransportError("Artifact HTTP transport failed") from None
    except BaseException:
        # This file is disposable by contract; never leave a partial ZIP.
        output.seek(0)
        output.truncate(0)
        raise
