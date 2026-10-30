"""Private codec worker HTTP contract; no Pillow dependency in Gateway CI."""

from __future__ import annotations

import hashlib
import http.client
import threading
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.site_media_worker.engine import Conversion
from app.site_media_worker.server import handler_class

TOKEN = "safe-private-worker-token-" + "0" * 40
BODY = b"\x89PNG\r\n\x1a\n" + b"\x00" * 48
ENCODED = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"data" * 8


@pytest.fixture
def worker_server():
    server = HTTPServer(("127.0.0.1", 0), handler_class(TOKEN))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=4)
        server.server_close()


def request(port: int, token: str, *, data=BODY, checksum=None, path="/v1/convert"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(
        "POST", path, body=data,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Media-Token": token,
            "X-Media-Source-Sha256": checksum or hashlib.sha256(data).hexdigest(),
            "X-Media-Codec": "webp",
            "X-Media-Quality": "100",
            "X-Media-Max-Width": "0",
            "X-Media-Min-PSNR": "36.0",
        },
    )
    result = connection.getresponse()
    content = result.read()
    headers = dict(result.getheaders())
    connection.close()
    return result.status, content, headers


def test_worker_requires_private_token_without_decoding(worker_server):
    with patch("app.site_media_worker.server.convert") as decoder:
        assert request(worker_server, "wrong")[0] == 403
        assert request(worker_server, TOKEN, checksum="0" * 64)[0] == 422
        assert request(worker_server, TOKEN, path="/not-a-converter")[0] == 404
    decoder.assert_not_called()


def test_worker_provides_hash_fenced_output_and_quality_metadata(worker_server):
    source_sha = hashlib.sha256(BODY).hexdigest()
    output_sha = hashlib.sha256(ENCODED).hexdigest()
    conversion = Conversion(
        content=ENCODED, codec="webp",
        width=1983, height=793, source_width=1983, source_height=793,
        psnr=99.0, source_sha256=source_sha, output_sha256=output_sha,
        quality=100, lossless=True,
    )
    with patch("app.site_media_worker.server.convert", return_value=conversion) as convert:
        status, content, hdrs = request(worker_server, TOKEN)
    assert status == 200
    assert content == ENCODED
    assert hdrs["X-Media-Source-Sha256"] == source_sha
    assert hdrs["X-Media-Output-Sha256"] == output_sha
    assert hdrs["X-Media-Width"] == "1983"
    assert hdrs["X-Media-PSNR"] == "99.0"
    assert hdrs["Cache-Control"] == "no-store"
    assert convert.call_args.kwargs == {
        "codec": "webp", "quality": 100, "max_width": 0, "min_psnr": 36.0,
    }


def test_gateway_media_overlay_is_client_only_no_embedded_worker():
    overlay = yaml.safe_load(Path("docker/site-media.compose.yml").read_text())
    assert set(overlay["services"]) == {"mcp-oauth"}
    client = overlay["services"]["mcp-oauth"]
    assert set(client["networks"]) == {"internal_net", "media_net"}
    assert client["environment"]["MEDIA_API_BASE_URL"] == "http://media-processing:8091"
    assert "SITE_MEDIA_WORKER_TOKEN" in client["environment"]
    assert overlay["networks"]["media_net"] == {
        "external": True, "name": "quart-media-internal",
    }
    # Deprecated local codec image is not deployed by the client-only overlay.
    # Its removal can follow actual external-service acceptance and rollback.
    base = Path("docker/Dockerfile.site-media-worker").read_text()
    assert "site_media_worker.selftest" in base


def test_worker_requires_sufficiently_long_secret():
    with pytest.raises(RuntimeError, match="at least 32"):
        handler_class("short")
