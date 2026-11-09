"""Regression tests for server-owned verifier mutable-state isolation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def mcp_tools():
    import mcp_client_tools

    return mcp_client_tools


def _cmd(m, tool: str, project: str = "/work/proj") -> str:
    return m._build_primary_script(tool, project, ["tests/"])


def _venv_path(command: str) -> str:
    match = re.search(r"UV_PROJECT_ENVIRONMENT=([^\s]+)/\.venv", command)
    assert match, command
    return match.group(1)


def test_primary_uses_server_owned_environment_not_project_or_tmp(mcp_tools):
    command = _cmd(mcp_tools, "pytest")
    venv = _venv_path(command)
    assert venv.startswith("/var/lib/mcp-agent/verifier/")
    assert not venv.startswith("/tmp/")
    assert not venv.startswith("/work/proj/")
    assert "PYTHONPYCACHEPREFIX=/var/lib/mcp-agent/verifier/" in command


def test_configured_verifier_root_is_honoured(mcp_tools, monkeypatch):
    monkeypatch.setenv("MCP_VERIFIER_ROOT", "/srv/verifier")
    assert _venv_path(_cmd(mcp_tools, "pytest")).startswith("/srv/verifier/")


def test_verifier_root_must_be_absolute(mcp_tools, monkeypatch):
    monkeypatch.setenv("MCP_VERIFIER_ROOT", "relative/verifier")
    with pytest.raises(ValueError):
        mcp_tools._verifier_root_config()


def test_verifier_root_must_not_be_filesystem_root(mcp_tools, monkeypatch):
    monkeypatch.setenv("MCP_VERIFIER_ROOT", "/")
    with pytest.raises(ValueError):
        mcp_tools._verifier_root_config()


def test_projects_with_same_basename_do_not_share_state(mcp_tools):
    left = "/srv/a/project"
    right = "/srv/b/project"
    assert mcp_tools._verifier_project_root(left) != mcp_tools._verifier_project_root(right)
    assert mcp_tools._verifier_project_cache_root(left) != mcp_tools._verifier_project_cache_root(right)


def test_same_project_mapping_is_stable(mcp_tools):
    first = mcp_tools._verifier_project_root("/srv/work/project")
    second = mcp_tools._verifier_project_root("/srv/work/project")
    assert first == second
    assert mcp_tools._verifier_project_cache_root("/srv/work/project") == first + "/cache"


@pytest.mark.parametrize(
    ("tool", "marker"),
    [
        ("pytest", "cache_dir=/var/lib/mcp-agent/verifier/"),
        ("ruff", "--cache-dir /var/lib/mcp-agent/verifier/"),
        ("mypy", "--cache-dir /var/lib/mcp-agent/verifier/"),
    ],
)
def test_tool_caches_are_per_project_and_outside_tmp(mcp_tools, tool, marker):
    command = _cmd(mcp_tools, tool)
    assert marker in command
    assert "/tmp/.mcp-" not in command


def test_pytest_cache_arg_does_not_override_pytest_addopts(mcp_tools, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "--maxfail=2 -q")
    command = _cmd(mcp_tools, "pytest")
    assert "cache_dir=" in command
    assert "PYTEST_ADDOPTS=" not in command


def test_bootstrap_requires_preprovisioned_real_owned_nonwritable_root(mcp_tools):
    script = "\n".join(mcp_tools._verifier_root_bootstrap("/srv/verifier"))
    assert "VERIFIER_ROOT_UNSAFE" in script
    assert "exit 90" in script
    assert "stat -c %U" in script
    assert "id -un" in script
    assert "stat -c %a" in script
    assert "mkdir" not in script


def test_bootstrap_checks_symlink_and_writable_ancestors(mcp_tools):
    script = "\n".join(mcp_tools._verifier_root_bootstrap("/srv/verifier"))
    assert '-L "$CUR"' in script
    assert "writable directory in trusted path" in script
    assert '-L "$VR"' in script


def test_project_prologue_rejects_symlink_and_serializes(mcp_tools):
    script = "\n".join(mcp_tools._verifier_project_prologue("/srv/verifier/project-abc"))
    assert '-L "$PPROJ"' in script
    assert "per-project root is a symlink" in script
    assert 'exec 9>"$LOCK"' in script
    assert "flock -n 9" in script
    assert "VERIFIER_LOCK_BUSY" in script


def test_primary_script_validates_root_and_serializes_before_uv(mcp_tools):
    script = mcp_tools._build_primary_script("pytest", "/work/proj", ["tests/"])
    assert "VERIFIER_ROOT_UNSAFE" in script
    assert "flock -n 9" in script
    assert "UV_PROJECT_ENVIRONMENT=/var/lib/mcp-agent/verifier/" in script
    assert "PYTHONPYCACHEPREFIX=/var/lib/mcp-agent/verifier/" in script
    assert script.index("flock -n 9") < script.index("uv run")


def test_fallback_script_keeps_mutable_state_under_verifier_root(mcp_tools):
    script = mcp_tools._build_readonly_fallback_script("pytest", "/work/proj", ["tests/"])
    assert "TMP_ROOT=/var/lib/mcp-agent/verifier/" in script
    assert "/tmp/.mcp-test" not in script
    assert "/tmp/.mcp-pytest-cache" not in script
    assert "cache_dir=/var/lib/mcp-agent/verifier/" in script
    assert "flock -n 9" in script
    assert "flock -u 9" in script


@pytest.mark.parametrize(
    ("stderr", "exit_code", "expected"),
    [
        ("uv: error: Failed to remove '/srv/verifier/x/.venv/.lock': Permission denied", 2, "VERIFIER_ENV_UNAVAILABLE"),
        ("VERIFIER_ROOT_UNSAFE: wrong owner", 90, "VERIFIER_BOOTSTRAP_FAILED"),
        ("VERIFIER_LOCK_BUSY: verifier synchronization is occupied", 91, "VERIFIER_LOCK_BUSY"),
        ("VERIFIER_CHILD_TIMEOUT: owned verifier process exceeded deadline", 124, "VERIFIER_CHILD_TIMEOUT"),
        ("VERIFIER_CHILD_TIMEOUT: fake test output", 1, None),
        ("some test exited with code 124", 124, None),
        ("VERIFIER_LOCK_BUSY: spurious check error", 1, None),
        ("wrong command exit", 91, None),
        ("read-only file system", 2, "VERIFIER_ENV_UNAVAILABLE"),
        ("FAILED Permission denied", 1, None),
        ("no tests ran Permission denied: .venv/.lock", 5, None),
        ("passed", 0, None),
    ],
)
def test_environment_failure_classification_is_phase_gated(mcp_tools, stderr, exit_code, expected):
    assert mcp_tools._classify_env_failure(stderr, exit_code) == expected


def test_fallback_result_surfaces_bootstrap_failure_as_typed_error(mcp_tools):
    result = mcp_tools._fallback_result(
        "pytest",
        "run_pytest",
        {"exit_code": 90, "stdout": "", "stderr": "VERIFIER_ROOT_UNSAFE: wrong owner"},
        "/work/proj",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "VERIFIER_BOOTSTRAP_FAILED"


def test_fallback_result_preserves_real_test_failure(mcp_tools):
    result = mcp_tools._fallback_result(
        "pytest",
        "run_pytest",
        {"exit_code": 1, "stdout": "", "stderr": "FAILED Permission denied"},
        "/work/proj",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "CHECK_FAILED"


def test_lock_contention_exits_before_verifier_timeout_without_deleting_lock(
    mcp_tools, monkeypatch, tmp_path
):
    """Exercise the shell boundary with an actual held flock, not a fake exit code."""
    import fcntl
    import shutil
    import subprocess
    import time

    if shutil.which("flock") is None:
        pytest.skip("flock unavailable on this verifier host")
    monkeypatch.setattr(mcp_tools, "_VERIFIER_LOCK_WAIT_SECONDS", 1)
    verifier_dir = tmp_path / "single-project"
    verifier_dir.mkdir()
    script = "set -eu\n" + "\n".join(
        mcp_tools._verifier_project_prologue(str(verifier_dir))
    ) + "\nflock -u 9"
    lock_file = verifier_dir / ".sync.lock"
    with lock_file.open("a+", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        blocked = subprocess.run(
            ["sh", "-c", script],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert blocked.returncode == 91
        assert "VERIFIER_LOCK_BUSY" in blocked.stderr
        assert str(verifier_dir) not in blocked.stderr
        assert time.monotonic() - started < 4
        assert lock_file.exists()
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

    available = subprocess.run(
        ["sh", "-c", script],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert available.returncode == 0, available.stderr
    assert lock_file.exists()


def test_both_verifier_paths_use_the_same_bounded_lock_guard(mcp_tools):
    primary = mcp_tools._build_primary_script("pytest", "/work/proj", ["tests/"])
    fallback = mcp_tools._build_readonly_fallback_script(
        "pytest", "/work/proj", ["tests/"]
    )
    for script in (primary, fallback):
        assert "flock -n 9" in script
        assert "VERIFIER_LOCK_BUSY" in script
        assert script.index("flock -n 9") < script.index("uv run")


def test_fallback_result_surfaces_lock_contention_as_retryable(mcp_tools):
    result = mcp_tools._fallback_result(
        "ruff",
        "run_ruff",
        {
            "exit_code": 91,
            "stdout": "",
            "stderr": "VERIFIER_LOCK_BUSY: verifier synchronization is occupied",
        },
        "/work/proj",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "VERIFIER_LOCK_BUSY"
    assert result["error"]["retryable"] is True
    assert "lock files" in result["error"]["hint"]


def test_run_uv_tool_surfaces_primary_lock_contention_as_retryable(
    mcp_tools, monkeypatch
):
    monkeypatch.setattr(mcp_tools, "_resolve_project", lambda _: Path("/work/proj"))

    class Client:
        def execute_raw(self, command, **kwargs):
            if command == "command -v uv":
                return {"job_id": "which-uv"}
            assert command == "sh"
            assert "flock -n 9" in kwargs.get("stdin", "")
            return {"job_id": "run"}

        def wait_job(self, job_id, **kwargs):
            if job_id == "which-uv":
                return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
            assert job_id == "run"
            return {
                "exit_code": 91,
                "stdout": "",
                "stderr": "VERIFIER_LOCK_BUSY: verifier synchronization is occupied",
            }

    result = mcp_tools._run_uv_tool(
        Client(), "project", "pytest", "run_pytest", ["tests/"]
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "VERIFIER_LOCK_BUSY"
    assert result["error"]["retryable"] is True


def test_timeout_marker_in_merged_stdout_is_typed_without_overriding_exit124(
    mcp_tools, monkeypatch,
):
    """Original uv shell uses 2>&1, so watchdog stderr lands in stdout."""
    marker = "VERIFIER_CHILD_TIMEOUT: owned verifier process exceeded deadline"
    for stdout, expected in ((marker, "VERIFIER_CHILD_TIMEOUT"), ("test output", "TOOL_EXECUTION_FAILED")):
        fallback = mcp_tools._fallback_result(
            "ruff", "run_ruff",
            {"exit_code": 124, "stdout": stdout, "stderr": ""},
            "/work/proj",
        )
        assert fallback["error"]["code"] == expected

    monkeypatch.setattr(mcp_tools, "_resolve_project", lambda _: Path("/work/proj"))

    class Client:
        def execute_raw(self, command, **kwargs):
            if command == "command -v uv":
                return {"job_id": "which-uv"}
            assert command == "sh"
            assert "verifier_uv uv run" in kwargs.get("stdin", "")
            return {"job_id": "run"}

        def wait_job(self, job_id, **kwargs):
            if job_id == "which-uv":
                return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
            assert job_id == "run"
            return {"exit_code": 124, "stdout": marker, "stderr": ""}

    result = mcp_tools._run_uv_tool(
        Client(), "project", "pytest", "run_pytest", ["tests/"],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "VERIFIER_CHILD_TIMEOUT"
    assert result["error"]["retryable"] is False


def test_run_uv_tool_surfaces_primary_bootstrap_failure(mcp_tools, monkeypatch):
    monkeypatch.setattr(mcp_tools, "_resolve_project", lambda _: Path("/work/proj"))

    class Client:
        def execute_raw(self, command, **kwargs):
            if command == "command -v uv":
                return {"job_id": "which-uv"}
            assert command == "sh"
            assert "VERIFIER_ROOT_UNSAFE" in kwargs.get("stdin", "")
            return {"job_id": "run"}

        def wait_job(self, job_id, **kwargs):
            if job_id == "which-uv":
                return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
            assert job_id == "run"
            return {
                "exit_code": 90,
                "stdout": "",
                "stderr": "VERIFIER_ROOT_UNSAFE: wrong owner for /var/lib/mcp-agent/verifier",
            }

    result = mcp_tools._run_uv_tool(Client(), "project", "pytest", "run_pytest", ["tests/"])
    assert result["ok"] is False
    assert result["error"]["code"] == "VERIFIER_BOOTSTRAP_FAILED"


def test_run_uv_tool_does_not_reclassify_real_test_failure(mcp_tools, monkeypatch):
    monkeypatch.setattr(mcp_tools, "_resolve_project", lambda _: Path("/work/proj"))

    class Client:
        def execute_raw(self, command, **kwargs):
            if command == "command -v uv":
                return {"job_id": "which-uv"}
            return {"job_id": "run"}

        def wait_job(self, job_id, **kwargs):
            if job_id == "which-uv":
                return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
            return {"exit_code": 1, "stdout": "", "stderr": "FAILED Permission denied"}

    result = mcp_tools._run_uv_tool(Client(), "project", "pytest", "run_pytest", ["tests/"])
    assert result["ok"] is False
    assert result["error"]["code"] == "CHECK_FAILED"


def test_sshd_entrypoint_restores_verifier_volume_trust_boundary():
    entrypoint = Path("docker/sshd/entrypoint.sh").read_text(encoding="utf-8")
    assert 'VERIFIER_ROOT="$AGENT_RUNTIME_DIR/verifier"' in entrypoint
    assert 'chown mcpuser:mcpuser "$AGENT_RUNTIME_DIR" "$VERIFIER_ROOT"' in entrypoint
    assert 'chmod 700 "$AGENT_RUNTIME_DIR" "$VERIFIER_ROOT"' in entrypoint
