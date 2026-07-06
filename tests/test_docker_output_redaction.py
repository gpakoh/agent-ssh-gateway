"""Tests for docker inspect output redaction."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "chatgpt_remote_mcp"))

from fleet.docker_client import DockerClient, REDACTED


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
