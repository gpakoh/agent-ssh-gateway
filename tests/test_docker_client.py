"""Tests for DockerClient compose path resolving and write operations."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "chatgpt_remote_mcp"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fleet.docker_client import COMPOSE_FILE_RE, CONTAINER_NAME_RE, SERVICE_NAME_RE, DockerClient


def _client() -> DockerClient:
    return DockerClient()


# ── Container name validation ──


def test_valid_container_names():
    for name in ["web", "my-app_1", "redis.cache", "a", "a" * 128]:
        assert CONTAINER_NAME_RE.match(name), f"should accept: {name}"


def test_invalid_container_names():
    for name in [
        "",
        "-leading-hyphen",
        ".leading-dot",
        "name;evil",
        "name&more",
        "name|pipe",
        "name$(id)",
        "name`id`",
        "../name",
        "name with space",
        "a" * 129,
    ]:
        assert not CONTAINER_NAME_RE.match(name), f"should reject: {name}"


# ── Service name validation ──


def test_valid_service_names():
    for name in ["web", "my-service", "api_gateway", "a", "a" * 64]:
        assert SERVICE_NAME_RE.match(name), f"should accept: {name}"


def test_invalid_service_names():
    for name in [
        "",
        "-leading-hyphen",
        ".leading-dot",
        "name;evil",
        "name/../",
        "name with space",
        "a" * 65,
    ]:
        assert not SERVICE_NAME_RE.match(name), f"should reject: {name}"


# ── Compose file name regex validation ──


def test_valid_compose_file_names():
    for name in [
        "docker-compose.yml",
        "compose.yaml",
        "deploy/docker-compose.yml",
        "a" * 256,
    ]:
        assert COMPOSE_FILE_RE.match(name), f"should accept: {name}"


def test_compose_file_re_accepts_dotdot():
    """COMPOSE_FILE_RE is format-only; path traversal caught separately."""
    assert COMPOSE_FILE_RE.match("../compose.yml")


def test_invalid_compose_file_names():
    for name in ["", "; rm -rf /", "a" * 257]:
        assert not COMPOSE_FILE_RE.match(name), f"should reject: {name}"


# ── Compose file_path rejection ──


def test_compose_ps_rejects_file_path():
    c = _client()
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        c.compose_ps(file_path="/some/path/docker-compose.yml")


def test_compose_services_rejects_file_path():
    c = _client()
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        c.compose_services(file_path="/some/path/docker-compose.yml")


@pytest.mark.asyncio
async def test_compose_up_rejects_file_path():
    c = _client()
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await c.compose_up(file_path="/some/path/docker-compose.yml")


@pytest.mark.asyncio
async def test_compose_restart_rejects_file_path():
    c = _client()
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await c.compose_restart(file_path="/some/path/docker-compose.yml")


@pytest.mark.asyncio
async def test_compose_build_rejects_file_path():
    c = _client()
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await c.compose_build(file_path="/some/path/docker-compose.yml")


@pytest.mark.asyncio
async def test_compose_logs_rejects_file_path():
    c = _client()
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await c.compose_logs(file_path="/some/path/docker-compose.yml")


@pytest.mark.asyncio
async def test_compose_down_rejects_file_path():
    c = _client()
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await c.compose_down(file_path="/some/path/docker-compose.yml")


# ── Compose project_dir validation ──


def test_compose_ps_validates_project_dir_exists():
    c = _client()
    with pytest.raises(ValueError, match="does not exist"):
        c._validate_project_dir("/nonexistent/path/xyz123")


def test_compose_ps_validates_project_dir_allowed_root():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError, match="outside allowed roots"):
            c._validate_project_dir(tmpdir)


def test_compose_ps_with_valid_project_dir():
    c = _client()
    c._validate_project_dir(None)  # None is always valid


def test_compose_base_argv_no_project_dir():
    c = _client()
    argv = c._compose_base_argv(project_dir=None)
    assert argv == ["/usr/bin/docker", "compose"]


def test_compose_base_argv_with_project_dir():
    c = _client()
    argv = c._compose_base_argv(project_dir="/some/path")
    assert argv == ["/usr/bin/docker", "compose", "--project-directory", "/some/path"]


# ── Container write operations (validation only, no real docker) ──


@pytest.mark.asyncio
async def test_start_invalid_container_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid container name"):
        await c.start("bad;name")


@pytest.mark.asyncio
async def test_stop_invalid_container_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid container name"):
        await c.stop("bad;name")


@pytest.mark.asyncio
async def test_restart_invalid_container_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid container name"):
        await c.restart("bad;name")


def test_restart_timeout_clamped():
    """restart clamps timeout to [1, 120]; does not raise."""
    c = _client()
    argv_high = c._restart_argv("web", timeout=121)
    assert "--time" in argv_high
    idx = argv_high.index("--time")
    assert argv_high[idx + 1] == "120"
    argv_low = c._restart_argv("web", timeout=0)
    idx = argv_low.index("--time")
    assert argv_low[idx + 1] == "1"


def test_stop_timeout_clamped():
    """stop clamps timeout to [1, 120]; does not raise."""
    c = _client()
    argv_high = c._stop_argv("web", timeout=121)
    idx = argv_high.index("--time")
    assert argv_high[idx + 1] == "120"
    argv_low = c._stop_argv("web", timeout=0)
    idx = argv_low.index("--time")
    assert argv_low[idx + 1] == "1"


# ── Compose write operations (validation only) ──


@pytest.mark.asyncio
async def test_compose_up_path_traversal_raises():
    c = _client()
    with pytest.raises(ValueError, match="does not exist"):
        await c.compose_up(project_dir="/tmp/../bad")


@pytest.mark.asyncio
async def test_compose_up_invalid_service_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid service name"):
        await c.compose_up(project_dir=None, services=["ok", "bad;name"])


@pytest.mark.asyncio
async def test_compose_restart_invalid_service_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid service name"):
        await c.compose_restart(project_dir=None, services=["bad;name"])


@pytest.mark.asyncio
async def test_compose_build_invalid_service_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid service name"):
        await c.compose_build(project_dir=None, services=["bad;name"])


@pytest.mark.asyncio
async def test_compose_logs_invalid_service_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid service name"):
        await c.compose_logs(project_dir=None, services=["bad;name"])


# ── Compose argv construction ──


def test_compose_base_argv_with_project_dir_tmp():
    c = _client()
    argv = c._compose_base_argv(project_dir="/tmp")
    assert argv == [
        "/usr/bin/docker",
        "compose",
        "--project-directory",
        "/tmp",
    ]


def test_compose_base_argv_without_project_dir():
    c = _client()
    argv = c._compose_base_argv(project_dir=None)
    assert argv == ["/usr/bin/docker", "compose"]


def test_compose_up_argv_detach():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        argv = c._compose_base_argv(project_dir=tmpdir)
        argv.append("up")
        argv.append("--detach")
        assert "--detach" in argv


def test_compose_build_argv_no_cache():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        argv = c._compose_base_argv(project_dir=tmpdir)
        argv.append("build")
        argv.append("--no-cache")
        assert "--no-cache" in argv


def test_compose_logs_argv_tail_clamped():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        argv = c._compose_base_argv(project_dir=tmpdir)
        argv.append("logs")
        argv.extend(["--tail", "1000"])
        assert "--tail" in argv
        assert "1000" in argv


# ── Admin operations validation ──


def test_validate_image_tag_valid():
    c = _client()
    for name in ["alpine:3.20", "python:3.11-slim", "busybox:1.36"]:
        assert c._validate_image_tag(name) == name


def test_validate_image_tag_invalid():
    c = _client()
    for name in ["alpine", "alpine:latest:extra", "image:tag:extra", "", "bad;image:tag"]:
        with pytest.raises(ValueError, match="Invalid image"):
            c._validate_image_tag(name)


def test_validate_image_ref_valid():
    c = _client()
    for name in ["alpine", "alpine:3.20", "python:3.11-slim"]:
        assert c._validate_image_ref(name) == name


def test_validate_volume_name_valid():
    c = _client()
    for name in ["data", "my_volume", "pgdata.01"]:
        assert c._validate_volume_name(name) == name


def test_validate_volume_name_invalid():
    c = _client()
    for name in ["", "bad;name", "../volume", "volume with space"]:
        with pytest.raises(ValueError, match="Invalid volume name"):
            c._validate_volume_name(name)


def test_validate_exec_argv_valid():
    c = _client()
    c._validate_exec_argv(["ls", "-la"])
    c._validate_exec_argv(["whoami"])
    c._validate_exec_argv(["cat", "/etc/hostname"])


def test_validate_exec_argv_empty():
    c = _client()
    with pytest.raises(ValueError, match="non-empty array"):
        c._validate_exec_argv([])


def test_validate_exec_argv_blocked_env():
    c = _client()
    with pytest.raises(ValueError, match="blocked pattern.*env"):
        c._validate_exec_argv(["env"])


def test_validate_exec_argv_blocked_shadow():
    c = _client()
    with pytest.raises(ValueError, match="blocked pattern"):
        c._validate_exec_argv(["cat", "/etc/shadow"])


def test_validate_exec_argv_blocked_shell_launcher():
    c = _client()
    with pytest.raises(ValueError, match="shell launcher blocked"):
        c._validate_exec_argv(["sh", "-c", "whoami"])
    with pytest.raises(ValueError, match="shell launcher blocked"):
        c._validate_exec_argv(["bash", "-c", "ls"])
    with pytest.raises(ValueError, match="shell launcher blocked"):
        c._validate_exec_argv(["ash", "-c", "id"])


def test_validate_exec_argv_blocked_ssh():
    c = _client()
    with pytest.raises(ValueError, match="blocked pattern"):
        c._validate_exec_argv(["cat", "/root/.ssh/authorized_keys"])


def test_prune_type_admin_accepts():
    c = _client()
    assert c._validate_prune_type("volume", admin_scope=True) == "volume"
    assert c._validate_prune_type("system", admin_scope=True) == "system"


def test_prune_type_admin_rejects_without_scope():
    c = _client()
    with pytest.raises(ValueError, match="Unsupported prune type"):
        c._validate_prune_type("volume")
    with pytest.raises(ValueError, match="Unsupported prune type"):
        c._validate_prune_type("system")


@pytest.mark.asyncio
async def test_rmi_too_many():
    c = _client()
    with pytest.raises(ValueError, match="1-5"):
        await c.rmi(["a"] * 6)


@pytest.mark.asyncio
async def test_rmi_invalid_ref():
    c = _client()
    with pytest.raises(ValueError, match="Invalid image"):
        await c.rmi(["bad;ref"])


@pytest.mark.asyncio
async def test_volume_rm_too_many():
    c = _client()
    with pytest.raises(ValueError, match="1-5"):
        await c.volume_rm(["a"] * 6)


@pytest.mark.asyncio
async def test_volume_rm_invalid_name():
    c = _client()
    with pytest.raises(ValueError, match="Invalid volume name"):
        await c.volume_rm(["bad;name"])


# ── Admin async methods (validation only) ──


@pytest.mark.asyncio
async def test_exec_argv_container_name_validated():
    c = _client()
    with pytest.raises(ValueError, match="Invalid container name"):
        await c.exec("bad;name", ["ls"])


@pytest.mark.asyncio
async def test_run_image_tag_required():
    c = _client()
    with pytest.raises(ValueError, match="tag required"):
        await c.run("alpine", ["whoami"])


@pytest.mark.asyncio
async def test_run_container_name_validated():
    c = _client()
    with pytest.raises(ValueError, match="Invalid container name"):
        await c.run("alpine:3.20", ["whoami"], container_name="bad;name")


def test_compose_down_volumes_argv():
    c = _client()
    argv = c._compose_base_argv(project_dir=None)
    argv.append("down")
    argv.append("--volumes")
    argv.extend(["-t", "30"])
    assert "--volumes" in argv


# ── _truncate_table_output ──

_HEADER = "NAMES\tIMAGE\tSTATUS\tPORTS"
_SEP = "--\t--\t--\t--"


def _table(lines: list[str]) -> str:
    return "\n".join([_HEADER, _SEP] + lines)


def test_truncate_no_truncation_needed():
    output = _table(["web\nginx:alpine\traunning", "db\tpostgres:16\traunning"])
    result = DockerClient._truncate_table_output(output, limit=50)
    assert result == output
    assert "showing" not in result


def test_truncate_limits_data_rows():
    rows = [f"app{i}\tnginx:{i}\traunning" for i in range(100)]
    output = _table(rows)
    result = DockerClient._truncate_table_output(output, limit=10)
    lines = result.splitlines()
    assert len(lines) == 2 + 10 + 1  # header + sep + 10 data rows + truncation notice
    assert "showing 10 of 100 results" in lines[-1]
    assert "use limit or filter" in lines[-1]


def test_truncate_empty_output():
    result = DockerClient._truncate_table_output("", limit=10)
    assert result == ""


def test_truncate_header_only():
    result = DockerClient._truncate_table_output(_HEADER + "\n" + _SEP, limit=10)
    lines = result.splitlines()
    assert len(lines) == 2
    assert "showing" not in result


def test_truncate_exact_boundary():
    rows = [f"app{i}\tnginx\traunning" for i in range(5)]
    output = _table(rows)
    result = DockerClient._truncate_table_output(output, limit=5)
    assert result == output
    assert "showing" not in result


def test_truncate_preserves_header_format():
    rows = [f"app{i}\tnginx\traunning" for i in range(30)]
    output = _table(rows)
    result = DockerClient._truncate_table_output(output, limit=5)
    lines = result.splitlines()
    assert lines[0] == _HEADER
    assert lines[1] == _SEP
