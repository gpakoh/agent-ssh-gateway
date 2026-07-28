"""Tests for docker inspect output redaction."""

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


def test_redacts_token_env():
    sanitized = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    data = json.loads(sanitized)
    env = data["Config"]["Env"]
    assert "TOKEN=<redacted>" in env
    assert "TOKEN=sk-abc123def456" not in env


def test_redacts_secret_env():
    sanitized = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    data = json.loads(sanitized)
    env = data["Config"]["Env"]
    assert "SECRET=<redacted>" in env
    assert "SECRET=super-secret-value" not in env


def test_redacts_password_env():
    sanitized = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    data = json.loads(sanitized)
    env = data["Config"]["Env"]
    assert "PASSWORD=<redacted>" in env
    assert "PASSWORD=hunter2" not in env


def test_redacts_jwt_env():
    sanitized = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    data = json.loads(sanitized)
    env = data["Config"]["Env"]
    assert "JWT=<redacted>" in env
    assert "eyJhbGciOiJIUzI1NiJ9" not in json.dumps(env)


def test_redacts_api_key_env():
    sanitized = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    data = json.loads(sanitized)
    env = data["Config"]["Env"]
    assert "API_KEY=<redacted>" in env
    assert "API_KEY=abcdef123456" not in env


def test_redacts_pgpassword_env():
    sanitized = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    data = json.loads(sanitized)
    env = data["Config"]["Env"]
    assert "PGPASSWORD=<redacted>" in env
    # 'PGPASSWORD' contains 'PASS' — should match
    assert "PGPASSWORD=db_secret_123" not in env


def test_keeps_benign_env():
    sanitized = _client()._sanitize_inspect_output(json.dumps(SAMPLE_INSPECT))
    data = json.loads(sanitized)
    env = data["Config"]["Env"]
    assert "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in env
    assert "MY_VAR=hello_world" in env
    assert "SOME_PATH=/safe/path" in env


def test_redacts_sensitive_dict_keys():
    payload = {"Labels": {"TOKEN": "abc", "safe_label": "visible"}}
    sanitized = _client()._sanitize_inspect_output(json.dumps(payload))
    data = json.loads(sanitized)
    assert data["Labels"]["TOKEN"] == REDACTED
    assert data["Labels"]["safe_label"] == "visible"


def test_redacts_nested_dict():
    payload = {
        "Config": {
            "safe_group": {"API_KEY": "super-secret", "URL": "http://example.com"},
        }
    }
    sanitized = _client()._sanitize_inspect_output(json.dumps(payload))
    data = json.loads(sanitized)
    assert data["Config"]["safe_group"]["API_KEY"] == REDACTED
    assert data["Config"]["safe_group"]["URL"] == "http://example.com"


def test_does_not_leak_original_value():
    payload = {"Env": ["SECRET=my_original_value"]}
    sanitized = _client()._sanitize_inspect_output(json.dumps(payload))
    assert "my_original_value" not in sanitized


def test_handles_non_json_output():
    raw = "docker: command not found"
    result = _client()._sanitize_inspect_output(raw)
    assert result == raw


def test_redacts_hostconfig_labels():
    payload = {
        "HostConfig": {
            "Labels": {
                "token": "secret123",
                "description": "safe-label",
            }
        }
    }
    sanitized = _client()._sanitize_inspect_output(json.dumps(payload))
    data = json.loads(sanitized)
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
    sanitized = _client()._sanitize_inspect_output(json.dumps(payload))
    data = json.loads(sanitized)
    assert data["Env"][0] == "ENCRYPTION_KEY=<redacted>"
    assert "cGxhaW50ZXh0LWZlcm5ldC1rZXk=" not in sanitized


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
    sanitized = _client()._sanitize_inspect_output(json.dumps(payload))
    data = json.loads(sanitized)
    assert "hunter2" not in sanitized
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
