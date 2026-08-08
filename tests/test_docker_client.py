"""Tests for DockerClient compose path resolving and write operations."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fleet.docker_client import (
    COMPOSE_FILE_RE,
    CONTAINER_NAME_RE,
    REDACTED,
    SERVICE_NAME_RE,
    DockerClient,
    RunResult,
)


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


@pytest.mark.asyncio
async def test_stats_uses_longer_timeout_than_default():
    """Regression: docker_stats was reported timing out at the default 30s
    SUBPROCESS_TIMEOUT on a host with many running containers — `stats
    --no-stream` samples live cgroup counters for every container, unlike
    cheap metadata-only reads like `ps`/`images`. Must use the same 60s
    budget as compose_ps, the other "enumerate everything" read.
    """
    c = _client()
    seen: dict[str, object] = {}

    async def _fake_run(argv, timeout=None, **kw):
        seen["timeout"] = timeout
        return "NAME\tCPU\n"

    c._run = _fake_run
    await c.stats()
    assert seen["timeout"] == 60.0


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


@pytest.mark.asyncio
async def test_compose_up_timeout_clamped():
    """Regression: compose_up's timeout flowed straight into the subprocess
    wait with no clamp, unlike stop/restart (which clamp to [1, 120]) —
    an absurd value hung the subprocess wait for that long. Same fix
    pattern, clamped to [1, 900] (compose up can legitimately take longer
    than a plain restart when --build is set).
    """
    c = _client()
    seen: dict[str, object] = {}

    async def _fake_run(argv, timeout=None, **kw):
        seen["timeout"] = timeout
        return ""

    c._run = _fake_run
    await c.compose_up(timeout=99999)
    assert seen["timeout"] == 900.0
    await c.compose_up(timeout=0)
    assert seen["timeout"] == 1.0


@pytest.mark.asyncio
async def test_compose_restart_timeout_clamped():
    c = _client()
    seen: dict[str, object] = {}

    async def _fake_run(argv, timeout=None, **kw):
        seen["timeout"] = timeout
        return ""

    c._run = _fake_run
    await c.compose_restart(timeout=99999)
    assert seen["timeout"] == 300.0
    await c.compose_restart(timeout=0)
    assert seen["timeout"] == 1.0


@pytest.mark.asyncio
async def test_compose_build_timeout_clamped():
    c = _client()
    seen: dict[str, object] = {}

    async def _fake_run(argv, timeout=None, **kw):
        seen["timeout"] = timeout
        return ""

    c._run = _fake_run
    await c.compose_build(timeout=99999)
    assert seen["timeout"] == 1800.0
    await c.compose_build(timeout=0)
    assert seen["timeout"] == 1.0


@pytest.mark.asyncio
async def test_compose_logs_timeout_clamped():
    c = _client()
    seen: dict[str, object] = {}

    async def _fake_run(argv, timeout=None, **kw):
        seen["timeout"] = timeout
        return ""

    c._run = _fake_run
    await c.compose_logs(timeout=99999)
    assert seen["timeout"] == 300.0
    await c.compose_logs(timeout=0)
    assert seen["timeout"] == 1.0


@pytest.mark.asyncio
async def test_compose_down_timeout_clamped():
    """compose_down's timeout also feeds the "docker compose down -t" argv
    directly, not just the subprocess wait — both must reflect the clamp.
    """
    c = _client()
    seen: dict[str, object] = {}

    async def _fake_run_with_result(argv, timeout=None, **kw):
        seen["timeout"] = timeout
        seen["argv"] = argv
        return RunResult(stdout="", stderr="", exit_code=0)

    c._run_with_result = _fake_run_with_result
    await c.compose_down(timeout=99999)
    assert seen["timeout"] == 310.0  # 300 clamp + 10
    argv = seen["argv"]
    idx = argv.index("-t")
    assert argv[idx + 1] == "300"

    await c.compose_down(timeout=0)
    assert seen["timeout"] == 11.0
    argv = seen["argv"]
    idx = argv.index("-t")
    assert argv[idx + 1] == "1"


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


# ── _parse_json_lines / _truncate_rows ──
#
# Docker ``--format json`` prints one JSON object per line (JSON Lines),
# not a wrapping array and not a "table" with a header row -- ps/images/
# stats/compose_ps now request this instead of a Go-template table string,
# so results come back as real structured rows instead of tab-separated
# text a client has to parse a second time.


def _jsonl(rows: list[dict]) -> str:
    import json as _json

    return "\n".join(_json.dumps(r) for r in rows)


def test_parse_json_lines_basic():
    output = _jsonl([{"Names": "web", "Image": "nginx"}, {"Names": "db", "Image": "postgres"}])
    result = DockerClient._parse_json_lines(output)
    assert result == [{"Names": "web", "Image": "nginx"}, {"Names": "db", "Image": "postgres"}]


def test_parse_json_lines_empty_output():
    assert DockerClient._parse_json_lines("") == []


def test_parse_json_lines_skips_blank_lines():
    output = "\n" + _jsonl([{"a": 1}]) + "\n\n"
    assert DockerClient._parse_json_lines(output) == [{"a": 1}]


def test_parse_json_lines_skips_malformed_line():
    """A single interleaved non-JSON line (e.g. a docker warning on
    stdout) must not blow up the whole listing -- it's dropped, the rest
    parses normally."""
    output = "not json at all\n" + _jsonl([{"a": 1}])
    assert DockerClient._parse_json_lines(output) == [{"a": 1}]


def test_parse_json_lines_skips_non_dict_json():
    output = "42\n" + _jsonl([{"a": 1}])
    assert DockerClient._parse_json_lines(output) == [{"a": 1}]


def test_truncate_rows_no_truncation_needed():
    rows = [{"i": i} for i in range(2)]
    truncated, total = DockerClient._truncate_rows(rows, limit=50)
    assert truncated == rows
    assert total == 2


def test_truncate_rows_limits_and_reports_total():
    rows = [{"i": i} for i in range(100)]
    truncated, total = DockerClient._truncate_rows(rows, limit=10)
    assert len(truncated) == 10
    assert truncated == rows[:10]
    assert total == 100


def test_truncate_rows_empty():
    truncated, total = DockerClient._truncate_rows([], limit=10)
    assert truncated == []
    assert total == 0


def test_truncate_rows_exact_boundary():
    rows = [{"i": i} for i in range(5)]
    truncated, total = DockerClient._truncate_rows(rows, limit=5)
    assert truncated == rows
    assert total == 5


def test_truncate_rows_negative_limit_returns_nothing():
    """Regression: a negative limit used to slice from the end
    (rows[:-1]) and return almost every row instead of limiting."""
    rows = [{"i": i} for i in range(5)]
    truncated, total = DockerClient._truncate_rows(rows, limit=-1)
    assert truncated == []
    assert total == 5


@pytest.mark.asyncio
async def test_ps_returns_structured_rows():
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        assert "--format" in argv
        assert argv[argv.index("--format") + 1] == "json"
        return _jsonl([{"Names": "web", "Image": "nginx:alpine", "Status": "Up"}])

    c._run = _fake_run
    result = await c.ps()
    assert result == [{"Names": "web", "Image": "nginx:alpine", "Status": "Up"}]


@pytest.mark.asyncio
async def test_ps_rejects_custom_go_template_format():
    """Regression (audit P0-1): arbitrary Go templates must not be accepted.

    docker_ps(format="{{.Labels}}") used to return raw docker output with
    unredacted absolute host paths, bypassing _sanitize_ps_row() and
    _truncate_rows(). The format param is gone from the client API, so a
    template can no longer reach docker at all.
    """
    c = _client()
    with pytest.raises(TypeError):
        await c.ps(format="{{.Labels}}")


@pytest.mark.asyncio
async def test_ps_always_requests_format_json():
    """The raw-return bypass branch is gone: ps() only ever asks docker for
    --format json output, which the sanitizer/truncator then process."""
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        assert "--format" in argv
        assert argv[argv.index("--format") + 1] == "json"
        assert "--format" not in [a for a in argv[argv.index("--format") + 1 :]]
        return _jsonl([{"Names": "web", "Image": "nginx:alpine", "Status": "Up"}])

    c._run = _fake_run
    result = await c.ps()
    assert result == [{"Names": "web", "Image": "nginx:alpine", "Status": "Up"}]


# ── last_truncated (pagination honesty) ──


@pytest.mark.asyncio
async def test_ps_sets_last_truncated_when_limited():
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        return _jsonl([{"Names": f"c{i}"} for i in range(20)])

    c._run = _fake_run
    result = await c.ps(limit=5)
    assert len(result) == 5
    assert c.last_truncated is True


@pytest.mark.asyncio
async def test_ps_last_truncated_false_within_limit():
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        return _jsonl([{"Names": f"c{i}"} for i in range(3)])

    c._run = _fake_run
    await c.ps(limit=50)
    assert c.last_truncated is False


@pytest.mark.asyncio
async def test_ps_sanitizes_labels_and_mounts_rows():
    """ps() applies row-level path redaction end to end."""
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        return _jsonl(
            [
                {
                    "Names": "web",
                    "Image": "nginx",
                    "Labels": (
                        "com.docker.compose.project=web,"
                        "com.docker.compose.project.config_files=/media/1TB/app/docker-compose.yml"
                    ),
                    "Mounts": "/media/1TB/app:/app",
                }
            ]
        )

    c._run = _fake_run
    result = await c.ps()
    row = result[0]
    assert REDACTED in row["Labels"]
    assert "/media/1TB/app/docker-compose.yml" not in row["Labels"]
    assert row["Mounts"] == REDACTED


@pytest.mark.asyncio
async def test_ps_sanitizes_multi_compose_config_files():
    """A single Labels value can hold several compose files separated by
    commas (config_files=/a.yml,/b.yml). Every path must be redacted, not
    just the first — the continuation chunk carries no '='."""
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        return _jsonl(
            [
                {
                    "Names": "web",
                    "Labels": (
                        "com.docker.compose.project=web,"
                        "com.docker.compose.project.config_files=/media/1TB/app/docker-compose.yml,"
                        "/media/1TB/app/docker-compose.override.yml"
                    ),
                }
            ]
        )

    c._run = _fake_run
    result = await c.ps()
    labels = result[0]["Labels"]
    assert "/media/1TB/app/docker-compose.yml" not in labels
    assert "/media/1TB/app/docker-compose.override.yml" not in labels
    assert labels.count(REDACTED) == 2


def test_sanitize_labels_string_non_path_comma_value_preserved():
    """Commas inside non-path label values (meta=foo,bar) are not paths and
    must stay untouched."""
    assert (
        DockerClient._sanitize_labels_string("app=web,meta=foo,bar,other=x")
        == "app=web,meta=foo,bar,other=x"
    )


@pytest.mark.asyncio
async def test_ps_reports_last_redacted():
    """meta.redacted must reflect that row sanitization actually changed
    something, not default to False while rows were redacted."""
    c = _client()

    async def _dirty_run(argv, timeout=None, **kw):
        return _jsonl(
            [{"Names": "web", "Labels": "com.docker.compose.project.config_files=/media/1TB/x.yml"}]
        )

    c._run = _dirty_run
    await c.ps()
    assert c.last_redacted is True

    async def _clean_run(argv, timeout=None, **kw):
        return _jsonl([{"Names": "web", "Status": "Up"}])

    c._run = _clean_run
    await c.ps()
    assert c.last_redacted is False


@pytest.mark.asyncio
async def test_inspect_reports_last_truncated():
    """inspect caps multi-container lists at max_lines; that cut must be
    reported the same way ps/compose_ps report theirs."""
    c = _client()
    entries = [{"Name": f"c{i}", "Id": f"id{i}"} for i in range(10)]

    async def _fake_run(argv, timeout=None, **kw):
        return json.dumps(entries)

    c._run = _fake_run
    data = await c.inspect("c0", max_lines=3)
    assert len(data) == 3
    assert c.last_truncated is True

    data = await c.inspect("c0", max_lines=None)
    assert len(data) == 10
    assert c.last_truncated is False


@pytest.mark.asyncio
async def test_images_returns_structured_rows():
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        return _jsonl([{"Repository": "nginx", "Tag": "alpine"}])

    c._run = _fake_run
    result = await c.images()
    assert result == [{"Repository": "nginx", "Tag": "alpine"}]


@pytest.mark.asyncio
async def test_logs_returns_structured_lines():
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        return "line one\nline two\nline three"

    c._run = _fake_run
    result = await c.logs("web")
    assert result == {"lines": ["line one", "line two", "line three"], "count": 3}


@pytest.mark.asyncio
async def test_logs_redacts_secrets_in_lines():
    """Regression: MAJOR finding from a live security audit. Container
    logs are one of the most likely places an application accidentally
    prints a real secret (a DSN with an embedded password, a bare
    KEY=value env dump) -- logs() returned every line completely
    unredacted."""
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        return (
            "starting up\n"
            "DATABASE_URL=postgresql://dbuser:hunter2@dbhost:5432/appdb\n"
            "ready"
        )

    c._run = _fake_run
    result = await c.logs("web")
    assert result["count"] == 3
    assert "hunter2" not in result["lines"][1]
    assert result["lines"][0] == "starting up"
    assert result["lines"][2] == "ready"


@pytest.mark.asyncio
async def test_compose_services_returns_structured_list():
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        return "web\ndb\nredis\n"

    c._run = _fake_run
    result = await c.compose_services()
    assert result == {"services": ["web", "db", "redis"], "count": 3}


@pytest.mark.asyncio
async def test_compose_logs_returns_structured_lines():
    c = _client()

    async def _fake_run(argv, timeout=None, **kw):
        return "web  | starting\ndb   | ready"

    c._run = _fake_run
    result = await c.compose_logs()
    assert result == {"lines": ["web  | starting", "db   | ready"], "count": 2}


def test_sanitize_labels_string_redacts_url_email_sha_values():
    """CI/registry URLs, emails and commit SHAs in label values must not
    leak infrastructure topology (audit T31 #11)."""
    labels = (
        "org.opencontainers.image.source=https://git.xloud.ru/gpakoh/web.git,"
        "org.opencontainers.image.authors=dev@example.com,"
        "org.opencontainers.image.revision=0123456789abcdef0123456789abcdef01234567,"
        "org.opencontainers.image.version=1.2.3"
    )
    out = DockerClient._sanitize_labels_string(labels)
    assert "git.xloud.ru" not in out
    assert "dev@example.com" not in out
    assert "0123456789abcdef" not in out
    assert "1.2.3" in out
    assert out.count(REDACTED) == 3
