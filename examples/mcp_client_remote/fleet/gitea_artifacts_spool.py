"""Ephemeral private storage for an exact Gitea Actions PR/run artifact.

This is an internal operator-side service, not an MCP tool. No path or ZIP
bytes are returned to a remote client. The spool does not publish, extract,
or proxy archives; the sole reader is a trusted in-process, side-effect-free
validator, and its output is never returned. A separate reviewed consumer
contract is required before any hosted-preview publication.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .gitea_artifacts_transport import (
    MAX_ZIP_BYTES,
    ArtifactTransportError,
    validate_zip_archive,
)

_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


@dataclass(frozen=True)
class ArtifactLease:
    owner: str
    repo: str
    pull_number: int
    run_id: int
    head_sha: str
    run_attempt: int
    artifact_id: int
    expected_sha256: str | None = None

    def validate(self) -> None:
        if any(
            not isinstance(s, str) or not _SEGMENT_RE.fullmatch(s) or ".." in s
            for s in (self.owner, self.repo)
        ):
            raise ArtifactTransportError("Invalid repository identity")
        for value in (self.pull_number, self.run_id, self.run_attempt, self.artifact_id):
            if type(value) is not int or value < 1:
                raise ArtifactTransportError("Invalid PR/run/artifact lease")
        if not isinstance(self.head_sha, str) or not _SHA_RE.fullmatch(self.head_sha):
            raise ArtifactTransportError("Invalid expected PR head")
        if self.expected_sha256 is not None and (
            not isinstance(self.expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.expected_sha256) is None
        ):
            raise ArtifactTransportError("Invalid expected artifact checksum")


def _private_spool_root(trusted_parent: Path, root: Path) -> Path:
    """Use a pre-provisioned 0700 root beneath a 0700 trusted parent.

    The directory names are operator-owned configuration, never MCP input.
    Both directories must be real, non-symlink, owned by the serving UID.
    """
    if (
        not trusted_parent.is_absolute()
        or not root.is_absolute()
        or root.parent != trusted_parent
    ):
        raise ArtifactTransportError("Private spool root is not an approved child")
    for path in (trusted_parent, root):
        try:
            info = path.lstat()
        except OSError:
            raise ArtifactTransportError("Private spool root is not provisioned") from None
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ArtifactTransportError("Private spool root has unsafe permissions")
    return root


async def _verify_exact_artifact(client: Any, lease: ArtifactLease) -> dict[str, Any]:
    """All five identity axes are exact; a missing attempt is never sufficient."""
    pr = await client.get_pull_request(lease.owner, lease.repo, lease.pull_number)
    if not isinstance(pr, dict) or type(pr.get("number")) is not int or (
        pr["number"] != lease.pull_number or pr.get("state") != "open"
        or pr.get("merged") is not False
    ):
        raise ArtifactTransportError("Artifact PR is not an open same-repository PR")
    head = pr.get("head")
    if (
        not isinstance(head, dict)
        or head.get("sha") != lease.head_sha
        or not isinstance(head.get("repo"), dict)
        or head["repo"].get("full_name") != f"{lease.owner}/{lease.repo}"
    ):
        raise ArtifactTransportError("Artifact PR HEAD or repository drifted")
    run = await client.get_action_run(lease.owner, lease.repo, lease.run_id)
    if (
        not isinstance(run, dict)
        or type(run.get("id")) is not int or run["id"] != lease.run_id
        or run.get("head_sha") != lease.head_sha
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] != lease.run_attempt
        or run.get("event") != "pull_request"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or not isinstance(run.get("repository"), dict)
        or run["repository"].get("full_name") != f"{lease.owner}/{lease.repo}"
    ):
        raise ArtifactTransportError("Exact artifact run evidence changed or is incomplete")
    page = await client.list_action_run_artifacts(lease.owner, lease.repo, lease.run_id)
    if not isinstance(page, dict):
        raise ArtifactTransportError("Artifact inventory is not trustworthy")
    items = page.get("artifacts")
    count = page.get("total_count")
    if (
        type(count) is not int or not 0 <= count <= 64
        or not isinstance(items, list) or len(items) != count
    ):
        raise ArtifactTransportError("Artifact inventory is incomplete")
    seen: set[int] = set()
    matched: dict[str, Any] | None = None
    for item in items:
        if not isinstance(item, dict):
            raise ArtifactTransportError("Artifact inventory item is invalid")
        artifact_id = item.get("id")
        provenance = item.get("workflow_run")
        if (
            type(artifact_id) is not int or artifact_id < 1 or artifact_id in seen
            or not isinstance(provenance, dict)
            or type(provenance.get("id")) is not int
            or provenance["id"] != lease.run_id
            or provenance.get("head_sha") != lease.head_sha
            or type(provenance.get("run_attempt")) is not int
            or provenance["run_attempt"] != lease.run_attempt
        ):
            raise ArtifactTransportError("Artifact provenance is not attempt-attested")
        seen.add(artifact_id)
        if artifact_id == lease.artifact_id:
            matched = item
    if matched is None:
        raise ArtifactTransportError("Exact artifact ID no longer exists")
    size = matched.get("size_in_bytes")
    if type(size) is not int or not 0 < size <= MAX_ZIP_BYTES:
        raise ArtifactTransportError("Artifact exceeds its bounded size")
    if matched.get("expired") is not False:
        raise ArtifactTransportError("Artifact is expired or its expiry is unknown")
    expiry = matched.get("expires_at")
    if not isinstance(expiry, str) or len(expiry) > 64:
        raise ArtifactTransportError("Artifact expiry is not attested")
    try:
        stamp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        raise ArtifactTransportError("Artifact expiry is not attested") from None
    if stamp.utcoffset() is None or stamp <= datetime.now(UTC):
        raise ArtifactTransportError("Artifact has expired")
    # Never return provider URLs, actor, or other untrusted raw metadata.
    return {"artifact_id": lease.artifact_id, "size_bytes": size, "expiry": expiry}


@dataclass
class _Entry:
    file: BinaryIO
    lease: ArtifactLease
    proof: dict[str, Any]
    expires_at: float
    timeout: asyncio.TimerHandle


class ArtifactSpool:
    """Process-local, bounded, auto-expiring, unnamed-file spool.

    A production caller constructs this exactly once with configured paths
    and only exposes *opaque* handles via a separate reviewed authorization.
    Each stage consumes a reservation before network I/O. Restarts revoke all
    handles automatically by closing their unnamed file descriptors.
    """

    def __init__(
        self, trusted_parent: Path, root: Path, *, ttl_s: int = 120, max_handles: int = 2
    ) -> None:
        if type(ttl_s) is not int or not 1 <= ttl_s <= 900:
            raise ArtifactTransportError("Invalid spool TTL")
        if type(max_handles) is not int or not 1 <= max_handles <= 8:
            raise ArtifactTransportError("Invalid spool capacity")
        self._root = _private_spool_root(trusted_parent, root)
        self._ttl_s = ttl_s
        self._max = max_handles
        self._lock = asyncio.Lock()
        self._pending = 0
        self._entries: dict[str, _Entry] = {}

    async def stage(
        self, client: Any, lease: ArtifactLease, *, redirect_origins: Collection[str]
    ) -> dict[str, Any]:
        """Time-bound the entire preflight/transport/postflight reservation.

        Per-read HTTP timeouts alone cannot bound a drip-fed archive or a
        stalled mock/provider. No stalled reservation may pin all spool slots
        indefinitely, even before a temporary inode has been created.
        """
        try:
            async with asyncio.timeout(self._ttl_s):
                return await self._stage_impl(
                    client, lease, redirect_origins=redirect_origins
                )
        except TimeoutError:
            raise ArtifactTransportError(
                "Private artifact staging deadline exceeded"
            ) from None

    async def _stage_impl(
        self, client: Any, lease: ArtifactLease, *, redirect_origins: Collection[str]
    ) -> dict[str, Any]:
        import secrets

        lease.validate()
        async with self._lock:
            if self._pending + len(self._entries) >= self._max:
                raise ArtifactTransportError("Private spool capacity is exhausted")
            self._pending += 1
        file: BinaryIO | None = None
        try:
            # The configured root may have been replaced since construction:
            # fail rather than create data in an untrusted directory.
            _private_spool_root(self._root.parent, self._root)
            before = await _verify_exact_artifact(client, lease)
            file = tempfile.TemporaryFile(mode="w+b", dir=self._root)
            file_info = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(file_info.st_mode)
                or stat.S_IMODE(file_info.st_mode) != 0o600
                or file_info.st_nlink != 0
            ):
                raise ArtifactTransportError("Private artifact inode is not isolated")
            report = await client.download_action_artifact_zip(
                lease.owner, lease.repo, lease.artifact_id, file,
                redirect_origins=redirect_origins,
                max_bytes=min(before["size_bytes"], MAX_ZIP_BYTES),
                expected_sha256=lease.expected_sha256,
            )
            if (
                not isinstance(report, dict)
                or type(report.get("size_bytes")) is not int
                or report["size_bytes"] != before["size_bytes"]
                or type(report.get("sha256")) is not str
                or re.fullmatch(r"[0-9a-f]{64}", report["sha256"]) is None
            ):
                raise ArtifactTransportError("Artifact bytes and metadata do not agree")
            # Never take even an internal client's claimed hash on faith: prove
            # the anonymous inode itself contains the complete expected ZIP.
            file.flush()  # Python BufferedRandom can defer writes until flush.
            if os.fstat(file.fileno()).st_size != before["size_bytes"]:
                raise ArtifactTransportError("Artifact private inode size mismatch")
            digest = hashlib.sha256()
            file.seek(0)
            while chunk := file.read(65536):
                digest.update(chunk)
            if digest.hexdigest() != report["sha256"] or (
                lease.expected_sha256 is not None
                and digest.hexdigest() != lease.expected_sha256
            ):
                raise ArtifactTransportError("Artifact private inode checksum mismatch")
            archive = validate_zip_archive(file)
            if (
                report.get("entries") != archive["entries"]
                or report.get("uncompressed_bytes", archive["uncompressed_bytes"])
                != archive["uncompressed_bytes"]
            ):
                raise ArtifactTransportError("Artifact ZIP directory metadata mismatch")
            file.seek(0)
            # Re-check both provenance and inventory after all private bytes
            # are received, then grant a short-lived one-time handle.
            after = await _verify_exact_artifact(client, lease)
            if after != before:
                raise ArtifactTransportError("Artifact metadata changed during transfer")
            loop = asyncio.get_running_loop()
            handle = secrets.token_urlsafe(24)
            expires = time.monotonic() + self._ttl_s
            async with self._lock:
                timer = loop.call_later(self._ttl_s, self.revoke, handle)
                self._entries[handle] = _Entry(
                    file=file, lease=lease,
                    proof={
                        "sha256": report["sha256"], "size_bytes": report["size_bytes"],
                        "artifact_id": lease.artifact_id,
                        "entries": report.get("entries"),
                    },
                    expires_at=expires, timeout=timer,
                )
                file = None
            # No paths, URLs or bytes. This object is intentionally NOT
            # authorization for a third-party consumer.
            return {"handle": handle, "sha256": report["sha256"], "size_bytes": report["size_bytes"], "ttl_s": self._ttl_s}
        except BaseException:
            if file is not None:
                file.close()
            raise
        finally:
            async with self._lock:
                self._pending -= 1

    def revoke(self, handle: str) -> bool:
        entry = self._entries.pop(handle, None)
        if entry is None:
            return False
        entry.timeout.cancel()
        entry.file.close()
        return True

    async def inspect_once(
        self,
        client: Any,
        handle: str,
        inspect: Callable[[BinaryIO], Awaitable[None]],
    ) -> dict[str, Any]:
        """One-time in-process inspection, never network or publication.

        The supplied inspector must be trusted and side-effect-free. A call
        consumes the handle before awaiting external evidence, and all errors,
        timeouts and cancellations close the file. No inspector output leaves
        this function. A publisher must use a separate staged/commit contract
        after independently establishing its own lease.
        """
        async with self._lock:
            entry = self._entries.pop(handle, None)
            if entry is not None:
                entry.timeout.cancel()
        if entry is None:
            raise ArtifactTransportError("Private artifact handle is unavailable")
        try:
            remaining = entry.expires_at - time.monotonic()
            if remaining <= 0:
                raise ArtifactTransportError("Private artifact handle expired")
            await asyncio.wait_for(_verify_exact_artifact(client, entry.lease), timeout=remaining)
            entry.file.seek(0)
            remaining = entry.expires_at - time.monotonic()
            if remaining <= 0:
                raise ArtifactTransportError("Private artifact handle expired")
            await asyncio.wait_for(inspect(entry.file), timeout=remaining)
            remaining = entry.expires_at - time.monotonic()
            if remaining <= 0:
                raise ArtifactTransportError("Private artifact handle expired")
            await asyncio.wait_for(_verify_exact_artifact(client, entry.lease), timeout=remaining)
            return dict(entry.proof)
        finally:
            entry.file.close()

    def close(self) -> None:
        for handle in tuple(self._entries):
            self.revoke(handle)
