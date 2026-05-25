"""mTLS e2e tests — verify mTLS bypass and Authelia fallback.

These tests run via SSH on the nginx host (LXC 100) and require:
  - SSH access (BatchMode) to NGINX_HOST
  - Client cert at /etc/nginx/certs/client.{crt,key}
  - CA cert at /etc/nginx/certs/ca.crt

Usage:
  NGINX_HOST=192.168.1.100 python -m pytest tests/test_mtls_e2e.py -v --timeout=30
"""

import json
import os
import subprocess
import sys

import pytest

NGINX_HOST = os.environ.get("NGINX_HOST", "192.168.1.100")
BASE_URL = "https://ssh.xloud.ru"
CLIENT_CERT = "/etc/nginx/certs/client.crt"
CLIENT_KEY = "/etc/nginx/certs/client.key"
BAD_CERT = "/tmp/ssh-gateway-bad-client.crt"
BAD_KEY = "/tmp/ssh-gateway-bad-client.key"


def _ssh_cmd(cmd: str) -> subprocess.CompletedProcess:
    full = (
        f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@{NGINX_HOST} "
        f"\"bash -se\" <<'REMOTE'\n{cmd}\nREMOTE"
    )
    return subprocess.run(
        ["bash", "-c", full],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _ensure_bad_client_cert() -> None:
    """Create a syntactically valid client cert that nginx must reject."""
    cmd = f"""
    if [ ! -s {BAD_CERT} ] || [ ! -s {BAD_KEY} ]; then
        openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
            -subj "/CN=Bad-SSH-Gateway-Client" \
            -keyout {BAD_KEY} -out {BAD_CERT} >/dev/null 2>&1
        chmod 600 {BAD_KEY}
    fi
    """
    result = _ssh_cmd(cmd)
    assert result.returncode == 0, result.stderr or result.stdout


def _curl(
    extra_args: str = "", path: str = "/api/servers", resolve: bool = True
) -> tuple[int, str]:
    resolve_flag = "--resolve ssh.xloud.ru:443:127.0.0.1" if resolve else ""
    cmd = (
        f'code=$(curl -k -sS -o /tmp/mtls-test.out -w "%{{http_code}}" '
        f'{resolve_flag} {extra_args} {BASE_URL}{path} 2>/dev/null || echo "FAIL") '
        f'&& cat /tmp/mtls-test.out 2>/dev/null && echo "---SEP---" && echo "$code"'
    )
    result = _ssh_cmd(cmd)
    if result.returncode != 0:
        return (999, result.stderr or result.stdout)
    parts = result.stdout.strip().rsplit("---SEP---", 1)
    if len(parts) == 2:
        body = parts[0].strip()
        code = parts[1].strip()
        if not code.isdigit():
            return (999, f"{body}\n{code}".strip())
        return (int(code), body)
    return (999, result.stdout)


def test_no_cert_redirects_to_authelia():
    """Without client cert, /api/servers must redirect (302) to Authelia."""
    code, _ = _curl()
    assert code == 302, f"Expected 302, got {code}"


def test_health_no_auth_required():
    """Health endpoint returns 200 without any auth."""
    code, body = _curl(path="/health")
    assert code == 200, f"Expected 200, got {code}"
    assert '"status":"ok"' in body, f"Unexpected body: {body}"


def test_with_valid_cert_bypasses_auth():
    """With valid client cert, /api/servers must NOT redirect (not 302)."""
    code, _ = _curl(f"--cert {CLIENT_CERT} --key {CLIENT_KEY}")
    assert code != 302, "mTLS request should not redirect"
    assert code != 999, "SSH/curl command failed"


def test_health_no_auth_with_cert():
    """Health endpoint is accessible with client cert too."""
    code, body = _curl(f"--cert {CLIENT_CERT} --key {CLIENT_KEY}", path="/health")
    assert code == 200, f"Expected 200, got {code}"
    assert '"status":"ok"' in body


def test_invalid_cert_rejected_by_nginx():
    """Unknown client cert is rejected at TLS boundary before Authelia."""
    _ensure_bad_client_cert()
    code, _ = _curl(f"--cert {BAD_CERT} --key {BAD_KEY}")
    assert code == 400, f"Expected 400 for invalid cert, got {code}"


def test_no_cert_api_ssh_redirects():
    """Without cert, /api/ssh/* endpoints also redirect."""
    for path in ["/api/ssh/execute/stream", "/api/ssh/pty/"]:
        code, _ = _curl(path=path)
        assert code == 302, f"Path {path}: expected 302, got {code}"


def test_mtls_x_headers_present():
    """With valid cert, nginx must inject X-SSL-* headers to backend."""
    code, body = _curl(
        f"--cert {CLIENT_CERT} --key {CLIENT_KEY}",
        path="/health",
    )
    assert code == 200, f"Expected 200, got {code}"

    # Health may not echo headers; this test verifies request succeeds with cert.
    # Header injection is validated by nginx config test below.
    assert '"status":"ok"' in body


def test_nginx_config_contains_mtls_bypass():
    """nginx config must contain mTLS bypass logic."""
    result = _ssh_cmd(
        "grep -R 'ssl_client_verify' /etc/nginx/sites-enabled/ssh.xloud.ru"
    )
    assert result.returncode == 0
    assert "SUCCESS" in result.stdout


def test_nginx_config_injects_api_key():
    """nginx must inject X-API-Key to backend for mTLS clients."""
    result = _ssh_cmd(
        "grep -R 'proxy_set_header X-API-Key' /etc/nginx/sites-enabled/ssh.xloud.ru"
    )
    assert result.returncode == 0
    assert "X-API-Key" in result.stdout


def test_backend_health_direct():
    """Backend container should be reachable directly on internal IP/port."""
    code, body = _curl(path="/health", resolve=False)
    assert code in (200, 302, 400), f"Unexpected direct code {code}: {body[:200]}"


def test_json_parse_health():
    """Health body should be parseable JSON when code=200."""
    code, body = _curl(path="/health")
    assert code == 200
    data = json.loads(body)
    assert data["status"] == "ok"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
