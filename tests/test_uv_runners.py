"""Tests for uv runner argv builder and target validation."""

import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"

sys.path.insert(0, str(EXAMPLE_DIR))
from mcp_client_tools import _build_uv_argv, _validate_targets  # noqa: E402


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
    result = _validate_targets("/project", ["/etc/passwd"])
    assert result == ["etc/passwd"]


class TestReadOnlyFallbackNoPathTraversal:
    """Bug 1: absolute target must not pass through _build_readonly_fallback_script."""

    def test_readonly_fallback_strips_absolute_targets(self):
        """Fallback script must convert /etc/passwd → <project_dir>/etc/passwd."""
        from mcp_client_tools import _build_readonly_fallback_script

        script = _build_readonly_fallback_script("pytest", "/project", ["/etc/passwd"])
        assert "/project/etc/passwd" in script
        last_line = [ln for ln in script.splitlines() if "pytest" in ln and "target_args" not in ln][-1]
        assert last_line.endswith("/project/etc/passwd 2>&1")

    def test_readonly_fallback_strips_absolute_targets_mypy(self):
        from mcp_client_tools import _build_readonly_fallback_script

        script = _build_readonly_fallback_script("mypy", "/project", ["/etc/shadow"])
        assert "/project/etc/shadow" in script

    def test_run_uv_tool_validates_targets_early(self, monkeypatch):
        """_run_uv_tool must validate targets before any SSH call."""
        from mcp_client_tools import _run_uv_tool

        call_log = []

        class FakeClient:
            def execute_raw(self, cmd, **kw):
                call_log.append(cmd)
                return {"job_id": "j1"}

            def wait_job(self, job_id, **kw):
                return {"exit_code": 0, "stdout": "", "stderr": ""}

        monkeypatch.setattr(
            "mcp_client_tools._resolve_project",
            lambda _: Path("/project"),
        )
        client = FakeClient()
        result = _run_uv_tool(client, "proj", "pytest", "project_run_pytest", target=["/etc/passwd"])

        assert result["ok"] is True
        assert "etc/passwd" in str(call_log)
        assert "/etc/passwd" not in str(call_log)
