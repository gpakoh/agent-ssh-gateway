"""Tests for docker inspect output redaction.

_sanitize_inspect_output() now returns the parsed, sanitized structure
directly (a list/dict) instead of re-serializing it back into a JSON
string for the caller to parse a second time.
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
        },
    },
    "HostConfig": {
        "Binds": ["/host/path:/container/path"],
    },
    "NetworkSettings": {
        "Ports": {"80/tcp": None, "443/tcp": None},
    },
}


def test_strips_config_env_entirely():
    """Config.Env is stripped entirely from docker inspect output."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    assert "Env" not in data.get("Config", {})


def test_redacts_hostconfig_binds():
    """HostConfig.Binds should be redacted (host paths exposed)."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    assert data["HostConfig"]["Binds"] == ["<redacted>"]
    assert "/host/path" not in json.dumps(data)


def test_keeps_other_config_fields():
    """Non-secret config fields survive sanitization."""
    data = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    assert data["Id"] == "abc123"
    assert data["Name"] == "/test-container"
    assert data["NetworkSettings"]["Ports"]["80/tcp"] is None


def test_redacts_sensitive_dict_keys():
    payload = {"Labels": {"TOKEN": "abc", "safe_label": "visible"}}
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert data["Labels"]["TOKEN"] == REDACTED
    assert data["Labels"]["safe_label"] == "visible"


def test_redacts_nested_dict():
    payload = {
        "Config": {
            "safe_group": {"API_KEY": "super-secret", "URL": "http://example.com"},
        }
    }
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert data["Config"]["safe_group"]["API_KEY"] == REDACTED
    assert data["Config"]["safe_group"]["URL"] == "http://example.com"


def test_does_not_leak_original_value():
    payload = {"Env": ["SECRET=my_original_value"]}
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert "my_original_value" not in json.dumps(data)


def test_handles_non_json_output():
    """Malformed/non-JSON docker output (e.g. "docker: command not found"
    on stderr-as-stdout) is wrapped as {"raw": ...} rather than raising --
    the return type is now always a structure, never a bare string."""
    raw = "docker: command not found"
    result = _client()._sanitize_inspect_output(raw)
    assert result == {"raw": raw}


def test_redacts_hostconfig_labels():
    payload = {
        "HostConfig": {
            "Labels": {
                "token": "secret123",
                "description": "safe-label",
            }
        }
    }
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert data["HostConfig"]["Labels"]["token"] == REDACTED
    assert data["HostConfig"]["Labels"]["description"] == "safe-label"


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
    explicitly, not a bare '...KEY' suffix.
    """
    payload = {"Env": ["ENCRYPTION_KEY=cGxhaW50ZXh0LWZlcm5ldC1rZXk="]}
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert data["Env"][0] == "ENCRYPTION_KEY=<redacted>"
    assert "cGxhaW50ZXh0LWZlcm5ldC1rZXk=" not in json.dumps(data)


def test_redacts_password_in_database_url_env():
    """Regression test: a DATABASE_URL/REDIS_URL-style connection string
    previously leaked its embedded password because the variable's own
    name doesn't contain PASSWORD/SECRET/TOKEN.
    """
    payload = {
        "Env": [
            "DATABASE_URL=postgresql://dbuser:hunter2@dbhost:5432/appdb",
            "REDIS_URL=redis://:hunter2@redishost:6379/0",
        ]
    }
    data = _client()._sanitize_inspect_output(json.dumps(payload))
    assert "hunter2" not in json.dumps(data)
    # Whole value redacted since the key name itself is URL-shaped.
    assert data["Env"][0] == "DATABASE_URL=<redacted>"
    assert data["Env"][1] == "REDIS_URL=<redacted>"


def test_redacts_dsn_credential_embedded_in_non_url_named_value():
    """A DSN-style credential embedded in a value whose key name is not
    itself URL/secret-shaped must still have its password redacted,
    while the rest of the connection string stays visible.
    """
    c = _client()
    result = c._sanitize_string("postgresql://dbuser:hunter2@dbhost:5432/appdb")
    assert "hunter2" not in result
    assert result == "postgresql://dbuser:<redacted>@dbhost:5432/appdb"


def test_dsn_redaction_does_not_touch_urls_without_credentials():
    c = _client()
    result = c._sanitize_string("http://example.com/path")
    assert result == "http://example.com/path"
