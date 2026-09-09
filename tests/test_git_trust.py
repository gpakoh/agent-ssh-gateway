from __future__ import annotations

from pathlib import Path

import pytest

from examples.mcp_server.git_trust import with_scoped_safe_directories


def _entries(env: dict[str, str]) -> list[tuple[str, str]]:
    count = int(env["GIT_CONFIG_COUNT"])
    return [
        (env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"])
        for index in range(count)
    ]


def test_scoped_safe_directories_are_inherited_without_global_config(tmp_path: Path) -> None:
    source = tmp_path / "source"
    git_dir = source / ".git"
    base = {
        "PATH": "/usr/bin",
        "HOME": str(tmp_path / "home"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "http.followRedirects",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
    }

    env = with_scoped_safe_directories((source, git_dir, source), base_env=base)

    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["HOME"] == base["HOME"]
    assert _entries(env) == [
        ("http.followRedirects", "false"),
        ("credential.helper", ""),
        ("safe.directory", str(source.resolve())),
        ("safe.directory", str(git_dir.resolve())),
    ]
    assert ("safe.directory", "*") not in _entries(env)
    assert base["GIT_CONFIG_COUNT"] == "2"


def test_scoped_safe_directories_reject_wildcard_and_inherited_wildcard(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exact path"):
        with_scoped_safe_directories(("*",), base_env={})

    inherited = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
    }
    with pytest.raises(ValueError, match="wildcard"):
        with_scoped_safe_directories((tmp_path,), base_env=inherited)


def test_scoped_safe_directories_replaces_stale_inherited_safe_directory(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    inherited = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(old),
        "GIT_CONFIG_KEY_1": "http.followRedirects",
        "GIT_CONFIG_VALUE_1": "false",
    }

    env = with_scoped_safe_directories((new,), base_env=inherited)

    assert ("safe.directory", str(old.resolve())) not in _entries(env)
    assert ("safe.directory", str(new.resolve())) in _entries(env)
    assert ("http.followRedirects", "false") in _entries(env)
