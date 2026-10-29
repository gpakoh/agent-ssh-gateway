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


def test_media_container_overlay_is_private_opt_in_and_resource_bounded():
    base = Path("docker/Dockerfile.site-media-worker").read_text()
    overlay = yaml.safe_load(Path("docker/site-media.compose.yml").read_text())
    service = overlay["services"]["site-media-worker"]
    assert service["profiles"] == ["media"]
    assert service["read_only"] is True
    assert service["mem_limit"] == "512m"
    assert service["cpus"] == 1
    assert service["pids_limit"] == 64
    assert "ports" not in service
    assert "volumes" not in service
    assert service["cap_drop"] == ["ALL"]
    assert overlay["networks"]["media_net"]["internal"] is True
    assert "mcp-oauth" in overlay["services"]
    assert "site_media_worker.selftest" in base
    assert 'Pillow==12.3.0' in base
    workflow = yaml.safe_load(Path('.github/workflows/ci.yml').read_text())
    media_job = workflow['jobs']['media-worker-qualify']
    assert media_job['timeout-minutes'] == 15
    assert media_job['needs'] == ['scope']
    assert 'media-worker-qualify' not in workflow['jobs']['deploy']['needs']


def test_worker_requires_sufficiently_long_secret():
    with pytest.raises(RuntimeError, match="at least 32"):
        handler_class("short")
