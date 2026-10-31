"""Media opt-in deploy gates and OAuth-to-Media runtime smoke regression tests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts/site_media_runtime_smoke.py"
SPEC = importlib.util.spec_from_file_location("site_media_runtime_smoke", PATH)
assert SPEC and SPEC.loader
smoke_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke_module)

TOKEN = "t" * 64
SHA = "a" * 40


class Response:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, count: int) -> bytes:
        return self.body[:count]


class Opener:
    def __init__(self, fail_path: str = "", mismatch_sha: bool = False):
        self.fail_path = fail_path
        self.mismatch_sha = mismatch_sha
        self.requests = []

    def open(self, request, timeout: int = 5):
        assert timeout == 5
        path = request.full_url.replace(smoke_module.MEDIA_BASE, "")
        self.requests.append(request)
        if path == self.fail_path:
            raise OSError("internal connectivity issue")
        if path == "/healthz":
            return Response(200, b"ok")
        if path == "/ready":
            return Response(200, json.dumps({"status": "ready", "build": SHA}).encode())
        if path == "/v1/capabilities":
            if request.get_header("X-media-token") == "z" * 513:
                raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                    request.full_url, 403, "denied", None, None
                )
            return Response(200, json.dumps({
                "build": "b" * 40 if self.mismatch_sha else SHA,
                "operations": [{"name": "raster.convert", "status": "available"}],
            }).encode())
        raise AssertionError(path)


def test_live_smoke_accepts_authenticated_media_and_denies_invalid_key():
    opener = Opener()
    with patch.object(smoke_module.urllib.request, "build_opener", return_value=opener):
        smoke_module.smoke({
            "MEDIA_API_BASE_URL": smoke_module.MEDIA_BASE,
            "SITE_MEDIA_WORKER_TOKEN": TOKEN,
        })
    assert len(opener.requests) == 4
    assert opener.requests[-1].get_header("X-media-token") == "z" * 513


@pytest.mark.parametrize("environment", [
    {},
    {"MEDIA_API_BASE_URL": "http://internal_net:8091", "SITE_MEDIA_WORKER_TOKEN": TOKEN},
    {"MEDIA_API_BASE_URL": smoke_module.MEDIA_BASE, "SITE_MEDIA_WORKER_TOKEN": "weak"},
])
def test_live_smoke_rejects_invalid_environment_before_network(environment):
    with pytest.raises(smoke_module.MediaSmokeError):
        smoke_module.smoke(environment)


@pytest.mark.parametrize("opener", [Opener(fail_path="/ready"), Opener(mismatch_sha=True)])
def test_live_smoke_fails_closed(opener):
    with patch.object(smoke_module.urllib.request, "build_opener", return_value=opener):
        with pytest.raises(smoke_module.MediaSmokeError):
            smoke_module.smoke({
                "MEDIA_API_BASE_URL": smoke_module.MEDIA_BASE,
                "SITE_MEDIA_WORKER_TOKEN": TOKEN,
            })


def test_ci_deploy_opt_in_and_first_attachment_rollback_contract():
    source = (ROOT / "scripts/deploy-from-registry.sh").read_text()
    assert 'SITE_MEDIA_OVERLAY_ENABLED="${SITE_MEDIA_OVERLAY_ENABLED:-false}"' in source
    assert 'COMPOSE="$BASE_COMPOSE -f docker/site-media.compose.yml"' in source
    assert 'python3 "$ROOT/scripts/site_media_preflight.py" || exit 1' in source
    assert 'docker exec mcp-oauth python3 scripts/site_media_runtime_smoke.py' in source
    assert 'SITE_MEDIA_FIRST_ATTACHMENT=true' in source
    assert 'COMPOSE="$BASE_COMPOSE"' in source
    assert 'SITE_MEDIA_OVERLAY_ENABLED=false' in source
    assert source.index('python3 "$ROOT/scripts/site_media_preflight.py"') < source.index('docker pull "$GATEWAY_REPO:$DEPLOY_TAG"')
