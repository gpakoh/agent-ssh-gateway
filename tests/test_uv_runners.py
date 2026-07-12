"""Tests for uv runner argv builder and target validation."""

import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"

sys.path.insert(0, str(EXAMPLE_DIR))
from chatgpt_tools import _build_uv_argv


def test_build_ruff_argv():
    argv = _build_uv_argv("ruff", "/project", ["src/"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "ruff", "check", "--", "src/",
    ]


def test_build_mypy_argv():
    argv = _build_uv_argv("mypy", "/project", ["src/main.py"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "mypy", "--", "src/main.py",
    ]


def test_build_pytest_argv():
    argv = _build_uv_argv("pytest", "/project", ["tests/"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "pytest", "--", "tests/",
    ]


def test_build_compileall_argv():
    argv = _build_uv_argv("compileall", "/project", ["src/"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "python", "-m", "compileall", "--", "src/",
    ]


def test_invalid_target_with_traversal():
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        _build_uv_argv("ruff", "/project", ["../outside"])


def test_invalid_target_absolute():
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        _build_uv_argv("ruff", "/project", ["/etc/passwd"])
