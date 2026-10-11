"""Approved operator-owned secret sources for guarded provisioning.

Secret bytes never cross the MCP tool boundary.  Callers select a logical
source id; this module resolves it to a fixed container path provisioned by the
operator/deploy plane and returns only the value internally plus a SHA-256
fingerprint for lease/postcondition evidence.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_MAX_SECRET_BYTES = 64 * 1024
_SECRET_SOURCE_ROOT = Path("/run/mcp-secret-sources")
_SECRET_SOURCE_FILES: dict[str, str] = {
    "registry_token": "registry_token",
}


class SecretSourceError(ValueError):
    """An approved secret source could not be proven safe/readable."""


@dataclass(frozen=True)
class ApprovedSecretSource:
    source_id: str
    value: str
    sha256: str


def _source_path(source_id: str) -> Path:
    if not isinstance(source_id, str):
        raise SecretSourceError("source_id must be a string")
    normalized = source_id.strip()
    filename = _SECRET_SOURCE_FILES.get(normalized)
    if filename is None:
        raise SecretSourceError("source_id is not approved")
    return _SECRET_SOURCE_ROOT / filename


def read_approved_secret_source(source_id: str) -> ApprovedSecretSource:
    """Read one fixed one-line secret source with strict filesystem guards.

    The source must be a regular file owned by the current process uid, with no
    group/other permission bits, no symlink traversal, and bounded size.  CR/LF
    terminators are stripped because the approved registry token is a line
    secret; embedded control characters are rejected.
    """

    path = _source_path(source_id)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SecretSourceError("approved secret source is unavailable") from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretSourceError("approved secret source is not a regular file")
        if info.st_uid != os.geteuid():
            raise SecretSourceError("approved secret source owner does not match runtime uid")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SecretSourceError("approved secret source permissions are too broad")
        if info.st_size < 1 or info.st_size > _MAX_SECRET_BYTES:
            raise SecretSourceError("approved secret source size is outside policy")

        chunks: list[bytes] = []
        remaining = _MAX_SECRET_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_SECRET_BYTES:
            raise SecretSourceError("approved secret source exceeds size policy")
    finally:
        os.close(fd)

    value_bytes = raw.rstrip(b"\r\n")
    if not value_bytes:
        raise SecretSourceError("approved secret source is empty")
    if any(byte < 0x20 or byte == 0x7F for byte in value_bytes):
        raise SecretSourceError("approved line secret contains control characters")
    try:
        value = value_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretSourceError("approved secret source is not UTF-8") from exc

    return ApprovedSecretSource(
        source_id=source_id.strip(),
        value=value,
        sha256=hashlib.sha256(value_bytes).hexdigest(),
    )
