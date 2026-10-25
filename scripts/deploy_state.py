#!/usr/bin/env python3
"""Fail-closed, content-bound LKG receipt for the SSH Gateway deploy script.

No automatic migration of legacy host checkout receipts is performed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import stat
import sys
from pathlib import Path

NAME = "web-ssh-gateway-deploy.json"
KEYS = frozenset({"gateway_image", "mcp_server_image", "sshd_image", "deploy_sha", "deployed_at"})
MAX_BYTES = 4096


class UntrustedState(ValueError):
    """The authoritative deployment receipt cannot safely be used."""


def _directory(raw: str) -> int:
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise UntrustedState("state directory must be an absolute non-root path")
    # A restrictive leaf is not enough if an attacker can replace it
    # through a writable checkout or parent. No symlinks along this chain.
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode):
            raise UntrustedState("state directory ancestor is not a real directory")
        if info.st_mode & 0o022:
            # Sticky, root-owned /tmp may hold a private per-UID tree:
            # other UIDs cannot rename/remove that tree.
            sticky_root = bool(info.st_mode & stat.S_ISVTX) and info.st_uid == 0
            if not sticky_root:
                raise UntrustedState("state directory has a group/world writable ancestor")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise UntrustedState("state directory must be owned by deploy UID with mode 0700")
    return fd


def _read(fd: int) -> dict[str, str] | None:
    try:
        handle = os.open(
            NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd,
        )
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(handle)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > MAX_BYTES
        ):
            raise UntrustedState("receipt is not a private, regular, single-linked 0600 file")
        with os.fdopen(handle, "rb", closefd=False) as data:
            raw = data.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise UntrustedState("receipt exceeds maximum size")
        document = json.loads(raw)
        if (
            not isinstance(document, dict)
            or document.keys() != KEYS
            or any(not isinstance(x, str) or not x for x in document.values())
        ):
            raise UntrustedState("receipt schema invalid")
        return document
    finally:
        os.close(handle)


def _write(fd: int, document: dict[str, str]) -> None:
    if document.keys() != KEYS:
        raise UntrustedState("invalid write schema")
    # Refuse to overwrite legacy/tampered receipt even if new JSON is valid.
    _read(fd)
    payload = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise UntrustedState("receipt output exceeds maximum size")
    temp_name = ".deploy-receipt-" + secrets.token_hex(16)
    handle = os.open(
        temp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600, dir_fd=fd,
    )
    committed = False
    try:
        with os.fdopen(handle, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(handle)
        os.replace(temp_name, NAME, src_dir_fd=fd, dst_dir_fd=fd)
        committed = True
        os.fsync(fd)
        if _read(fd) != document:
            raise UntrustedState("committed receipt did not verify")
    finally:
        os.close(handle)
        if not committed:
            try:
                os.unlink(temp_name, dir_fd=fd)
            except FileNotFoundError:
                pass


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight", "read", "write"])
    parser.add_argument("directory")
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args(argv)
    fd = _directory(args.directory)
    try:
        if args.action == "preflight":
            if args.arguments:
                raise UntrustedState("preflight takes no arguments")
            _read(fd)
        elif args.action == "read":
            if len(args.arguments) != 1 or args.arguments[0] not in KEYS:
                raise UntrustedState("read expects one receipt field")
            existing = _read(fd)
            print(existing[args.arguments[0]] if existing else "")
        else:
            if len(args.arguments) != 4:
                raise UntrustedState("write expects gateway, MCP, SSHD and deploy SHA")
            gateway, mcp, sshd, sha = args.arguments
            document = {
                "gateway_image": gateway,
                "mcp_server_image": mcp,
                "sshd_image": sshd,
                "deploy_sha": sha,
                "deployed_at": dt.datetime.now(dt.UTC).isoformat(),
            }
            _write(fd, document)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (UntrustedState, OSError, ValueError, UnicodeError) as exc:
        print(f"DEPLOY_STATE_UNTRUSTED: {exc}", file=sys.stderr)
        sys.exit(1)
