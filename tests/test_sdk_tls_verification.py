"""HTTPS certificate-chain and hostname verification contract of the Python SDK.

Loopback HTTPS server is isolated; no host credentials or live Gateway calls.
"""

from __future__ import annotations

import datetime as dt
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from sdk.ssh_gateway import SSHGatewayClient


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b'{"valid":true,"auth_mode":"api_key"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "test-ca.pem"
    key_path = tmp_path / "test-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, cert_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_https_verification_enabled_by_default() -> None:
    client = SSHGatewayClient("https://gateway.example.com")
    assert client.session.verify is True
    client.session.close()


def test_untrusted_tls_certificate_rejected(tls_server: tuple[int, Path]) -> None:
    port, _ = tls_server
    client = SSHGatewayClient(f"https://localhost:{port}")
    client.session.trust_env = False
    with pytest.raises(requests.exceptions.SSLError):
        client.auth_check()
    client.session.close()


def test_explicit_trusted_ca_is_accepted(tls_server: tuple[int, Path]) -> None:
    port, ca = tls_server
    client = SSHGatewayClient(f"https://localhost:{port}", ca_bundle=ca)
    client.session.trust_env = False
    assert client.session.verify == str(ca.resolve())
    assert client.auth_check()["valid"] is True
    client.session.close()


def test_trusted_ca_does_not_bypass_hostname_check(tls_server: tuple[int, Path]) -> None:
    port, ca = tls_server
    client = SSHGatewayClient(f"https://127.0.0.1:{port}", ca_bundle=ca)
    client.session.trust_env = False
    with pytest.raises(requests.exceptions.SSLError):
        client.auth_check()
    client.session.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://gateway.example.com",
        "http://192.0.2.10",
        "ftp://gateway.example.com",
        "https://username:password@gateway.example.com",
        "https://",
    ],
)
def test_insecure_or_malformed_remote_url_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        SSHGatewayClient(url)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_plaintext_loopback_is_allowed_for_local_testing(host: str) -> None:
    client = SSHGatewayClient(f"http://{host}:8085")
    assert client.session.verify is True
    client.session.close()


def test_ca_override_requires_existing_bundle(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing CA bundle"):
        SSHGatewayClient("https://gateway.example.com", ca_bundle=tmp_path / "missing.pem")
    with pytest.raises(ValueError, match="existing CA bundle"):
        SSHGatewayClient("https://gateway.example.com", ca_bundle=tmp_path)


def test_sdk_import_does_not_silence_certificate_warnings() -> None:
    # Use an isolated interpreter to avoid unrelated global warning filters.
    code = (
        "import warnings; "
        "from urllib3.exceptions import InsecureRequestWarning; "
        "import sdk.ssh_gateway; "
        "warnings.warn('TLS-warning-canary', InsecureRequestWarning)"
    )
    result = subprocess.run(
        [sys.executable, "-Wdefault", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        # Shared CI runners may delay interpreter startup under parallel shards.
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "TLS-warning-canary" in result.stderr
