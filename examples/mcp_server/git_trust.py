"""Process-local Git trust helpers for registered repository access.

Git's ``safe.directory`` setting is consulted by Git subprocesses that can be
spawned internally by commands such as ``git clone --local``. Command-line
``git -c safe.directory=...`` options do not reliably cross that subprocess
boundary, so trusted control-plane code uses the environment-backed Git config
protocol instead. The configuration is inherited by child Git processes but is
never persisted to system, global, or repository config.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

_MAX_GIT_CONFIG_ENTRIES = 64


def with_scoped_safe_directories(
    directories: Iterable[str | Path],
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an env whose inherited Git config trusts only exact directories.

    Existing non-``safe.directory`` environment-backed Git config entries are
    preserved (for example one-shot HTTPS auth settings). Existing
    ``safe.directory`` entries are removed rather than accumulated across
    operations, and wildcard trust is rejected fail-closed. No on-disk Git
    configuration is read or modified by this helper.
    """

    env = dict(os.environ if base_env is None else base_env)
    raw_count = str(env.get("GIT_CONFIG_COUNT", "0") or "0").strip()
    try:
        count = int(raw_count, 10)
    except ValueError as exc:
        raise ValueError("invalid inherited GIT_CONFIG_COUNT") from exc
    if count < 0 or count > _MAX_GIT_CONFIG_ENTRIES:
        raise ValueError("inherited GIT_CONFIG_COUNT is out of bounds")

    preserved: list[tuple[str, str]] = []
    for index in range(count):
        key_name = f"GIT_CONFIG_KEY_{index}"
        value_name = f"GIT_CONFIG_VALUE_{index}"
        key = env.get(key_name)
        value = env.get(value_name)
        if key is None or value is None:
            raise ValueError("incomplete inherited Git config environment")
        if key.strip().lower() == "safe.directory":
            if value.strip() == "*":
                raise ValueError("wildcard Git safe.directory trust is forbidden")
            continue
        preserved.append((key, value))

    safe_directories: list[str] = []
    for directory in directories:
        raw = str(directory).strip()
        if not raw or raw == "*":
            raise ValueError("Git safe.directory must be an exact path")
        resolved = str(Path(raw).resolve())
        if resolved == "*" or "\x00" in resolved:
            raise ValueError("Git safe.directory must be an exact path")
        if resolved not in safe_directories:
            safe_directories.append(resolved)

    entries = [*preserved, *(("safe.directory", path) for path in safe_directories)]
    if len(entries) > _MAX_GIT_CONFIG_ENTRIES:
        raise ValueError("scoped Git config entry limit exceeded")

    for index in range(count):
        env.pop(f"GIT_CONFIG_KEY_{index}", None)
        env.pop(f"GIT_CONFIG_VALUE_{index}", None)
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(len(entries))
    return env


__all__ = ["with_scoped_safe_directories"]
