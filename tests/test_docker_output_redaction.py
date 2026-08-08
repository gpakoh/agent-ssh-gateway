"""Tests for docker inspect/ps output redaction.

_sanitize_inspect_output() reduces docker inspect JSON to a strict
allowlist (host topology — GraphDriver paths, ResolvConfPath, PID, IPs,
MACs, network/endpoint IDs, compose working dirs — is dropped, not
blacklisted field by field) and then runs generic key-level secret
redaction on what remains. `docker ps` rows get host paths in their
Labels/Mounts strings redacted the same way.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"))

from fleet.docker_client import REDACTED, DockerClient


def _client() -> DockerClient:
    return DockerClient()


SAMPLE_INSPECT = {
    "Id": "abc123",
    "Name": "/test-container",
    "Created": "2026-01-01T00:00:00Z",
    "Config": {
        "Env": [
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TOKEN=sk-abc123def456",
            "SECRET=super-secret-value",
            "PASSWORD=hunter2",
            "JWT=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "API_KEY=abcdef123456",
            "PGPASSWORD=db_secret_123",
            "MY_VAR=hello_world",
            "SOME_PATH=/safe/path",
        ],
        "Labels": {
            "maintainer": "user@example.com",
            "com.docker.compose.project": "my-project",
            "com.docker.compose.project.config_files": "/media/1TB/Docker/compose/proj/docker-compose.yml",
            "com.docker.compose.project.working_dir": "/media/1TB/Docker/compose/proj",
        },
    },
    "HostConfig": {
        "Binds": ["/host/path:/container/path"],
        "NetworkMode": "bridge",
    },
    "Mounts": [
        {"Type": "bind", "Source": "/host/path", "Destination": "/container/path", "RW": True},
        {"Type": "volume", "Name": "my_volume", "Destination": "/data", "RW": True},
    ],
    "NetworkSettings": {
        "Ports": {"80/tcp": None, "443/tcp": None},
        "Networks": {
            "bridge": {
                "NetworkID": "net123",
                "EndpointID": "ep123",
                "Gateway": "172.17.0.1",
                "IPAddress": "172.17.0.2",
                "MacAddress": "02:42:ac:11:00:02",
            }
        },
        "IPAddress": "172.17.0.2",
        "MacAddress": "02:42:ac:11:00:02",
    },
    "GraphDriver": {
        "Data": {
            "LowerDir": "/media/1TB/Docker/overlay2/abc/lower",
            "MergedDir": "/media/1TB/Docker/overlay2/abc/merged",
            "UpperDir": "/media/1TB/Docker/overlay2/abc/upper",
            "WorkDir": "/media/1TB/Docker/overlay2/abc/work",
        },
        "Name": "overlay2",
    },
    "State": {
        "Status": "running",
        "Running": True,
        "Pid": 12345,
        "ExitCode": 0,
        "StartedAt": "2026-01-01T00:00:00Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
        "Health": {"Status": "healthy", "FailingStreak": 0, "Log": [{"ExitCode": 0}]},
    },
    "ResolvConfPath": "/media/1TB/Docker/containers/abc/resolv.conf",
    "HostnamePath": "/media/1TB/Docker/containers/abc/hostname",
    "HostsPath": "/media/1TB/Docker/containers/abc/hosts",
    "LogPath": "/media/1TB/Docker/containers/abc/xyz.log",
    "Path": "/bin/sh",
    "Args": ["-c", "echo hi"],
}


def test_strips_config_env_entirely():
    """Config.Env is stripped entirely from docker inspect output."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    assert "Env" not in data.get("Config", {})


def test_redacts_hostconfig_binds():
    """HostConfig.Binds should be redacted (host paths exposed)."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    assert data["HostConfig"]["Binds"] == [REDACTED]
    assert "/host/path" not in json.dumps(data)


def test_keeps_other_config_fields():
    """Non-secret config fields survive sanitization."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    assert data["Id"] == "abc123"
    assert data["Name"] == "/test-container"
    assert data["NetworkSettings"]["Ports"]["80/tcp"] is None


# ── Strict allowlist: host topology is dropped, not blacklisted ─────


def test_graph_driver_paths_dropped():
    """GraphDriver carries overlay host paths — the whole section is gone."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    assert "GraphDriver" not in data
    assert "overlay2" not in json.dumps(data)


def test_container_path_fields_dropped():
    """ResolvConfPath/HostnamePath/HostsPath/LogPath leak host paths."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    for key in ("ResolvConfPath", "HostnamePath", "HostsPath", "LogPath"):
        assert key not in data


def test_state_pid_dropped():
    """State.Pid reveals host-side process info."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    assert "Pid" not in data["State"]
    assert data["State"]["Status"] == "running"
    assert data["State"]["Running"] is True
    assert data["State"]["Health"]["Status"] == "healthy"
    assert "Log" not in data["State"]["Health"]


def test_network_topology_dropped():
    """Internal IPs, MACs, network/endpoint IDs and gateways are dropped;
    only the published-port map survives."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    assert "Networks" not in data["NetworkSettings"]
    assert "IPAddress" not in data["NetworkSettings"]
    assert "MacAddress" not in data["NetworkSettings"]
    for needle in ("172.17.0.1", "172.17.0.2", "02:42:ac:11:00:02", "net123", "ep123"):
        assert needle not in json.dumps(data)
    assert data["NetworkSettings"]["Ports"]["443/tcp"] is None


def test_compose_path_labels_redacted():
    """Compose stamps absolute host paths into Config.Labels; the label
    name survives, the path value is redacted."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    labels = data["Config"]["Labels"]
    assert labels["com.docker.compose.project"] == "my-project"
    assert labels["com.docker.compose.project.config_files"] == REDACTED
    assert labels["com.docker.compose.project.working_dir"] == REDACTED
    assert "/media/1TB/Docker/compose" not in json.dumps(data)


def test_mounts_source_redacted_but_destination_kept():
    """Mounts[*].Source is a host path; the container-side Destination and
    the volume Name stay visible."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    mounts = data["Mounts"]
    assert mounts[0]["Source"] == REDACTED
    assert mounts[0]["Destination"] == "/container/path"
    assert mounts[1]["Name"] == "my_volume"
    assert mounts[1]["Destination"] == "/data"


def test_path_args_dropped():
    """Path/Args are not in the allowlist and are dropped."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    assert "Path" not in data
    assert "Args" not in data


# ── Generic key-level redaction on what remains ─────────────────────


def test_redacts_sensitive_labels_keys():
    payload = {"Config": {"Labels": {"TOKEN": "abc", "safe_label": "visible"}}}
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert data["Config"]["Labels"]["TOKEN"] == REDACTED
    assert data["Config"]["Labels"]["safe_label"] == "visible"


def test_redacts_sensitive_nested_keys():
    payload = {"Config": {"Labels": {"safe_group": {"API_KEY": "super-secret"}}}}
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert data["Config"]["Labels"]["safe_group"]["API_KEY"] == REDACTED


def test_does_not_leak_original_value():
    payload = {"Config": {"Env": ["SECRET=my_original_value"]}}
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert "Env" not in data.get("Config", {})
    assert "my_original_value" not in json.dumps(data)


def test_handles_non_json_output():
    """Malformed/non-JSON docker output (e.g. "docker: command not found"
    on stderr-as-stdout) is wrapped as {"raw": ...} rather than raising."""
    raw = "docker: command not found"
    result = _client()._sanitize_inspect_output(raw)
    assert result == {"raw": raw}


def test_unknown_hostconfig_keys_dropped():
    """Keys outside the HostConfig allowlist are dropped (previously a
    synthetic HostConfig.Labels was preserved by the blacklist)."""
    payload = {"HostConfig": {"Labels": {"token": "secret123"}, "NetworkMode": "host"}}
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert "Labels" not in data["HostConfig"]
    assert data["HostConfig"]["NetworkMode"] == "host"


def test_single_object_not_list():
    """A non-list inspect payload (single dict) stays a dict."""
    data = _client()._sanitize_inspect_output(json.dumps({"Id": "x", "Name": "/c"}))
    assert isinstance(data, dict)
    assert data["Id"] == "x"


def test_is_sensitive_key():
    c = _client()
    assert c._is_sensitive_key("TOKEN")
    assert c._is_sensitive_key("API_KEY")
    assert c._is_sensitive_key("JWT")
    assert c._is_sensitive_key("AUTHORIZATION")
    assert c._is_sensitive_key("CLIENT_SECRET")
    assert c._is_sensitive_key("ACCESS_KEY")
    assert not c._is_sensitive_key("MY_VAR")
    assert not c._is_sensitive_key("HOSTNAME")
    assert not c._is_sensitive_key("PATH")


def test_redacts_encryption_key_env():
    """Regression test: ENCRYPTION_KEY previously leaked unredacted because
    the key-name regex only matched API_KEY/PRIVATE_KEY/ACCESS_KEY
    explicitly, not a bare '...KEY' suffix."""
    payload = {"Config": {"Env": ["ENCRYPTION_KEY=cGxhaW50ZXh0LWZlcm5ldC1rZXk="]}}
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert "Env" not in data.get("Config", {})
    assert "cGxhaW50ZXh0LWZlcm5ldC1rZXk=" not in json.dumps(data)


def test_redacts_password_in_database_url_env():
    """Regression test: a DATABASE_URL/REDIS_URL-style connection string
    previously leaked its embedded password because the variable's own
    name doesn't contain PASSWORD/SECRET/TOKEN."""
    payload = {
        "Config": {
            "Env": [
                "DATABASE_URL=postgresql://dbuser:hunter2@dbhost:5432/appdb",
                "REDIS_URL=redis://:hunter2@redishost:6379/0",
            ]
        }
    }
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert "Env" not in data.get("Config", {})
    assert "hunter2" not in json.dumps(data)


def test_redacts_dsn_credential_embedded_in_non_url_named_value():
    """A DSN-style credential embedded in a value whose key name is not
    itself URL/secret-shaped must still have its password redacted."""
    c = _client()
    result = c._sanitize_string("postgresql://dbuser:hunter2@dbhost:5432/appdb")
    assert "hunter2" not in result
    assert result == "postgresql://dbuser:<redacted>@dbhost:5432/appdb"


def test_dsn_redaction_does_not_touch_urls_without_credentials():
    c = _client()
    result = c._sanitize_string("http://example.com/path")
    assert result == "http://example.com/path"


# ── docker ps row sanitization (Labels/Mounts host paths) ───────────


def test_ps_labels_string_paths_redacted():
    row = {
        "Names": "web",
        "Image": "nginx",
        "Labels": (
            "com.docker.compose.project=web,"
            "com.docker.compose.project.config_files=/media/1TB/Docker/web/docker-compose.yml,"
            "com.docker.compose.project.working_dir=/media/1TB/Docker/web"
        ),
        "Mounts": "",
    }
    out = DockerClient._sanitize_ps_row(row)
    assert out["Labels"] == (
        "com.docker.compose.project=web,"
        f"com.docker.compose.project.config_files={REDACTED},"
        f"com.docker.compose.project.working_dir={REDACTED}"
    )


def test_ps_mounts_bind_path_redacted():
    row = {"Names": "web", "Image": "nginx", "Labels": "", "Mounts": "/media/1TB/app:/app"}
    out = DockerClient._sanitize_ps_row(row)
    assert out["Mounts"] == REDACTED


def test_ps_mounts_volume_name_kept():
    row = {
        "Names": "db",
        "Image": "postgres",
        "Labels": "",
        "Mounts": "pgdata:/var/lib/postgresql/data",
    }
    out = DockerClient._sanitize_ps_row(row)
    assert out["Mounts"] == "pgdata:/var/lib/postgresql/data"


def test_ps_mounts_named_volume_first_still_redacts_later_path():
    """Regression: MAJOR finding from a live security audit, confirmed live
    on this exact host (mcp-server's own `docker ps` row): Mounts was only
    redacted when the string STARTED with "/" -- docker orders mounts
    arbitrarily, so "named_volume,/real/host/path" left the real path
    unredacted whenever a named volume happened to sort first.
    """
    row = {
        "Names": "mcp-server",
        "Image": "mcp-server:latest",
        "Labels": "",
        "Mounts": "web-ssh-gateway_data,/deploy/web-ssh-gateway,46244ecad691da",
    }
    out = DockerClient._sanitize_ps_row(row)
    assert "/deploy/web-ssh-gateway" not in out["Mounts"]
    assert out["Mounts"] == f"web-ssh-gateway_data,{REDACTED},46244ecad691da"
