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
    assert "flock 9" in script


def test_primary_script_validates_root_and_serializes_before_uv(mcp_tools):
    script = mcp_tools._build_primary_script("pytest", "/work/proj", ["tests/"])
    assert "VERIFIER_ROOT_UNSAFE" in script
    assert "flock 9" in script
    assert "UV_PROJECT_ENVIRONMENT=/var/lib/mcp-agent/verifier/" in script
    assert "PYTHONPYCACHEPREFIX=/var/lib/mcp-agent/verifier/" in script
    assert script.index("flock 9") < script.index("uv run")


def test_fallback_script_keeps_mutable_state_under_verifier_root(mcp_tools):
    script = mcp_tools._build_readonly_fallback_script("pytest", "/work/proj", ["tests/"])
    assert "TMP_ROOT=/var/lib/mcp-agent/verifier/" in script
    assert "/tmp/.mcp-test" not in script
    assert "/tmp/.mcp-pytest-cache" not in script
    assert "cache_dir=/var/lib/mcp-agent/verifier/" in script
    assert "flock 9" in script
    assert "flock -u 9" in script


@pytest.mark.parametrize(
    ("stderr", "exit_code", "expected"),
    [
        ("uv: error: Failed to remove '/srv/verifier/x/.venv/.lock': Permission denied", 2, "VERIFIER_ENV_UNAVAILABLE"),
        ("VERIFIER_ROOT_UNSAFE: wrong owner", 90, "VERIFIER_BOOTSTRAP_FAILED"),
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
