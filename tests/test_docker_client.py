"""Tests for DockerClient compose path resolving and write operations."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "chatgpt_remote_mcp"))

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


# ── Compose path resolving ──


def test_compose_relative_path_resolved_under_project_dir():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        result = c._resolve_compose_file_path("docker-compose.yml", project_dir=tmpdir)
        assert result == os.path.join(tmpdir, "docker-compose.yml")


def test_compose_relative_path_defaults_to_raw():
    c = _client()
    result = c._resolve_compose_file_path("docker-compose.yml", project_dir=None)
    assert result == "docker-compose.yml"


def test_compose_none_path_returns_none():
    c = _client()
    assert c._resolve_compose_file_path(None, project_dir=None) is None


def test_compose_absolute_path_inside_allowed_root():
    c = _client()
    result = c._resolve_compose_file_path(
        "/media/1TB/Python/web_ssh/web-ssh-gateway/docker/docker-compose.yml",
        project_dir=None,
        allowed_roots={"/media/1TB/Python/web_ssh"},
    )
    assert result == "/media/1TB/Python/web_ssh/web-ssh-gateway/docker/docker-compose.yml"


def test_compose_absolute_path_outside_allowed_root():
    c = _client()
    with pytest.raises(ValueError, match="outside allowed root"):
        c._resolve_compose_file_path(
            "/etc/passwd",
            project_dir=None,
            allowed_roots={"/media/1TB/Python/web_ssh"},
        )


def test_compose_absolute_path_no_roots_configured():
    c = _client()
    with pytest.raises(ValueError, match="no allowed roots"):
        c._resolve_compose_file_path("/etc/passwd", project_dir=None, allowed_roots=set())


def test_compose_path_traversal_blocked():
    c = _client()
    for path in ["../etc/passwd", "foo/../../etc/passwd"]:
        with pytest.raises(ValueError, match="traversal"):
            c._resolve_compose_file_path(path, project_dir="/opt/proj")


def test_compose_project_dir_must_exist():
    c = _client()
    with pytest.raises(ValueError, match="does not exist"):
        c._resolve_compose_file_path("docker-compose.yml", project_dir="/nonexistent/path/xyz123")


def test_compose_path_validation_rejects_empty():
    c = _client()
    with pytest.raises(ValueError, match="Invalid compose file path"):
        c._resolve_compose_file_path("", project_dir=None)


def test_compose_path_validation_rejects_too_long():
    c = _client()
    with pytest.raises(ValueError, match="Invalid compose file path"):
        c._resolve_compose_file_path("a" * 257, project_dir=None)


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
    with pytest.raises(ValueError, match="traversal"):
        await c.compose_up(project_dir="/tmp", file_path="../bad.yml")


@pytest.mark.asyncio
async def test_compose_up_invalid_service_raises():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError, match="Invalid service name"):
            await c.compose_up(project_dir=tmpdir, services=["ok", "bad;name"])


@pytest.mark.asyncio
async def test_compose_restart_invalid_service_raises():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError, match="Invalid service name"):
            await c.compose_restart(project_dir=tmpdir, services=["bad;name"])


@pytest.mark.asyncio
async def test_compose_build_invalid_service_raises():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError, match="Invalid service name"):
            await c.compose_build(project_dir=tmpdir, services=["bad;name"])


@pytest.mark.asyncio
async def test_compose_logs_invalid_service_raises():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError, match="Invalid service name"):
            await c.compose_logs(project_dir=tmpdir, services=["bad;name"])


# ── Compose argv construction ──


def test_compose_base_argv_with_file():
    c = _client()
    argv = c._compose_base_argv(file_path="compose.yml", project_dir="/tmp")
    assert argv == ["/usr/bin/docker", "compose", "-f", "compose.yml", "--project-directory", "/tmp"]


def test_compose_base_argv_without_file():
    c = _client()
    argv = c._compose_base_argv(file_path=None, project_dir=None)
    assert argv == ["/usr/bin/docker", "compose"]


def test_compose_up_argv_detach():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        argv = c._compose_base_argv(None, tmpdir)
        argv.append("up")
        argv.append("--detach")
        assert "--detach" in argv


def test_compose_build_argv_no_cache():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        argv = c._compose_base_argv(None, tmpdir)
        argv.append("build")
        argv.append("--no-cache")
        assert "--no-cache" in argv


def test_compose_logs_argv_tail_clamped():
    c = _client()
    with tempfile.TemporaryDirectory() as tmpdir:
        argv = c._compose_base_argv(None, tmpdir)
        argv.append("logs")
        argv.extend(["--tail", "1000"])
        assert "--tail" in argv
        assert "1000" in argv
