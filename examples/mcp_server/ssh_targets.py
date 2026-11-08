"""Operator-owned SSH target aliases for MCP -> Gateway execution.

Target selection never accepts a caller-provided IP, key path, password or user.
Gateway's existing /api/ssh/connect and execute-argv enforce the target CIDR,
host-key verification, session ownership, command policy and audit boundary.
This adapter adds *no* alternative direct SSH or arbitrary remote shell.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway_client import GatewayClient

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$", re.ASCII)
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$", re.ASCII)
_MAX_CONFIG_BYTES = 32768
_MAX_KEY_BYTES = 65536
_MAX_TARGETS = 16


class TargetConfigError(ValueError):
    """Reject malformed or untrusted target configuration without fallback."""


@dataclass(frozen=True)
class Target:
    name: str
    host: str
    port: int
    username: str
    key_file: Path


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, val in pairs:
        if key in obj:
            raise TargetConfigError("Duplicate SSH target configuration key")
        obj[key] = val
    return obj


def _read_bounded_regular_file(path: Path, max_bytes: int, *, private: bool) -> bytes:
    """Reject symlinks, world-writable config and exposed private key files.

    O_NOFOLLOW protects the final component; every directory between the
    registered root and file is checked by the configured bind-mount owner.
    Files are mounted read-only by deployment, so they cannot be swapped by
    untrusted MCP callers after validation.
    """
    if not path.is_absolute():
        raise TargetConfigError("SSH target file must be an absolute path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            st = os.fstat(handle.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size < 1 or st.st_size > max_bytes:
                raise TargetConfigError("SSH target file must be a bounded regular file")
            if st.st_mode & (0o077 if private else 0o022):
                raise TargetConfigError("Unsafe SSH target file permissions")
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise TargetConfigError("SSH target file is inaccessible") from exc
    if not data or len(data) > max_bytes:
        raise TargetConfigError("SSH target file size is invalid")
    return data


def load_targets(
    *,
    config_file: str | None = None,
    key_dir: str | None = None,
) -> dict[str, Target]:
    """Return an immutable-in-practice resolved mapping of approved SSH hosts.

    No configuration -> empty map, not an implicit default SSH host.
    Host must be a literal IP (no DNS re-resolution, URLs or aliases).
    Key location is constrained to the operator's mounted key directory.
    """
    filename = config_file if config_file is not None else os.getenv("MCP_SSH_TARGETS_FILE", "")
    if not filename:
        return {}
    root = Path(key_dir if key_dir is not None else os.getenv(
        "MCP_SSH_TARGET_KEY_DIR", "/run/ssh-targets/keys"
    ))
    if not root.is_absolute() or root.is_symlink():
        raise TargetConfigError("SSH target key directory is invalid")
    config = Path(filename)
    try:
        source = _read_bounded_regular_file(config, _MAX_CONFIG_BYTES, private=False)
        data = json.loads(source.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, UnicodeError) as exc:
        raise TargetConfigError("Invalid SSH target configuration") from exc

    if not isinstance(data, dict) or set(data) != {"version", "targets"}:
        raise TargetConfigError("Invalid SSH target configuration shape")
    if type(data["version"]) is not int or data["version"] != 1:
        raise TargetConfigError("Unsupported SSH target configuration version")
    definitions = data["targets"]
    if not isinstance(definitions, dict) or len(definitions) > _MAX_TARGETS:
        raise TargetConfigError("Invalid SSH target count")

    targets: dict[str, Target] = {}
    for name, entry in definitions.items():
        if not isinstance(name, str) or not _ALIAS_RE.fullmatch(name):
            raise TargetConfigError("Invalid SSH target name")
        if not isinstance(entry, dict) or set(entry) != {
            "host", "port", "username", "key_file"
        }:
            raise TargetConfigError("Invalid SSH target entry shape")
        host, port, username, key_file = (
            entry["host"], entry["port"], entry["username"], entry["key_file"]
        )
        if not isinstance(host, str):
            raise TargetConfigError("SSH target IP must be a literal address")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise TargetConfigError("SSH target IP must be a literal address") from exc
        if ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local:
            raise TargetConfigError("Unsupported SSH target IP range")
        if type(port) is not int or not 1 <= port <= 65535:
            raise TargetConfigError("Invalid SSH target port")
        if not isinstance(username, str) or not _USER_RE.fullmatch(username):
            raise TargetConfigError("Invalid SSH target user")
        if not isinstance(key_file, str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,64}", key_file, flags=re.ASCII
        ):
            raise TargetConfigError("Invalid SSH target key reference")
        key_path = root / key_file
        _read_bounded_regular_file(key_path, _MAX_KEY_BYTES, private=True)
        targets[name] = Target(
            name=name, host=str(ip), port=port, username=username, key_file=key_path
        )
    return targets


def target_inventory() -> list[dict[str, Any]]:
    """List declared names, never private keys or their filesystem paths."""
    return [
        {"name": name, "host": t.host, "port": t.port, "username": t.username}
        for name, t in sorted(load_targets().items())
    ]


def execute_target(
    *,
    target: str,
    argv: list[str],
    stdin: str = "",
    timeout_s: int = 30,
    client_factory: Callable[..., GatewayClient] = GatewayClient,
) -> dict[str, Any]:
    """Run argv through the existing Gateway policy, never an unguarded SSH subprocess.

    A new short-lived, non-persistent session is created per invocation.
    Failed requests do not reuse a different target's previous SID.
    """
    if not isinstance(target, str) or not _ALIAS_RE.fullmatch(target):
        raise TargetConfigError("Invalid SSH target name")
    if not isinstance(argv, list) or not argv or any(
        not isinstance(arg, str) or "\x00" in arg for arg in argv
    ) or sum(len(arg.encode("utf-8")) for arg in argv) > 65536:
        raise TargetConfigError("Invalid argv for SSH target")
    if not isinstance(stdin, str) or len(stdin.encode("utf-8")) > 1_048_576:
        raise TargetConfigError("Invalid stdin for SSH target")
    if type(timeout_s) is not int or not 1 <= timeout_s <= 120:
        raise TargetConfigError("SSH target command timeout must be 1..120 seconds")
    targets = load_targets()
    profile = targets.get(target)
    if profile is None:
        raise TargetConfigError("Unknown or unconfigured SSH target")

    client = client_factory(
        session_id="",
        ssh_host=profile.host,
        ssh_port=profile.port,
        ssh_user=profile.username,
        ssh_password="",
        ssh_private_key="",
        ssh_key_path=str(profile.key_file),
    )
    # An ephemeral, non-reusable logical session keeps target identities
    # separate even when the same user or key is assigned to two hosts.
    client._reuse_existing = False
    client._ephemeral = True
    client._idle_timeout_seconds = 60
    try:
        client.connect()
        raw = client.execute_argv(argv=argv, stdin=stdin, timeout_s=timeout_s)
    finally:
        client.disconnect()
    return raw
