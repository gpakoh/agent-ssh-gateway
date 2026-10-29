"""Project website asset transport: raw-byte parity, policy and CAS security."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.workspace.models import ProjectInfo
from app.workspace.registry import WorkspaceRegistry

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF" + b"\x01\x00\x00\x00" + b"WEBP" + b"not-a-real-image"
BASE = "/api/workspace/projects/candidate-xloud-site-asset-test/assets"


@pytest.fixture
def assets(monkeypatch, tmp_path: Path):
    candidate = tmp_path / "candidate"
    source = tmp_path / "source"
    candidate.mkdir()
    source.mkdir()
    (candidate / "public" / "visuals").mkdir(parents=True)
    (candidate / "public" / "fonts").mkdir(parents=True)
    (source / "public" / "visuals").mkdir(parents=True)
    entries = {
        "candidate-xloud-site-asset-test": ProjectInfo(
            project_id="candidate-xloud-site-asset-test", root=candidate,
            type="app", description="test isolated candidate", tags=[],
        ),
        "xloud": ProjectInfo(
            project_id="xloud", root=source,
            type="app", description="production source checkout", tags=[],
        ),
    }
    registry = WorkspaceRegistry(
        entries, [tmp_path],
        granted_scopes={"project:read", "project:write", "workspace:read", "workspace:write"},
    )
    monkeypatch.setattr("app.routers.workspace.get_registry", lambda: registry)
    monkeypatch.setattr(settings, "api_key", "asset-test-key")
    monkeypatch.setattr(settings, "workspace_readonly", False)
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda request, trusted: "127.0.0.1"
    )
    return TestClient(app), candidate, source


def _url(path="public/visuals/banner.png", *, extra=""):
    return BASE + "/upload?path=" + path + extra


def _put(client, payload=PNG, *, path="public/visuals/banner.png", **kwargs):
    sha = kwargs.pop("sha", hashlib.sha256(payload).hexdigest())
    extra = kwargs.pop("extra", "")
    return client.put(
        _url(path, extra=extra) + "&sha256=" + sha,
        content=payload,
        headers={"X-API-Key": "asset-test-key", "Content-Type": kwargs.pop("mime", "image/png")},
        **kwargs,
    )


def test_raw_upload_inspect_download_exact_sha_and_bytes(assets):
    client, candidate, _ = assets
    uploaded = _put(client)
    assert uploaded.status_code == 200, uploaded.text
    result = uploaded.json()
    assert result["verified"] is True
    assert result["size"] == len(PNG)
    assert result["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert result["previous_sha256"] is None
    assert (candidate / "public/visuals/banner.png").read_bytes() == PNG

    info = client.get(
        BASE + "/inspect?path=public/visuals/banner.png",
        headers={"X-API-Key": "asset-test-key"},
    )
    assert info.status_code == 200
    assert info.json()["sha256"] == result["sha256"]
    download = client.get(
        BASE + "/download?path=public/visuals/banner.png",
        headers={"X-API-Key": "asset-test-key"},
    )
    assert download.status_code == 200
    assert download.content == PNG
    assert download.headers["cache-control"] == "private, no-store"
    assert download.headers["x-content-type-options"] == "nosniff"


def test_overwrite_requires_previous_digest_and_is_compare_and_swap(assets):
    client, candidate, _ = assets
    old = _put(client)
    assert old.status_code == 200
    conflict = _put(client, payload=PNG + b"new")
    assert conflict.status_code == 409
    assert (candidate / "public/visuals/banner.png").read_bytes() == PNG
    wrong = _put(client, payload=PNG + b"new", extra="&previous_sha256=" + "f" * 64)
    assert wrong.status_code == 409
    good = _put(
        client, payload=PNG + b"new",
        extra="&previous_sha256=" + hashlib.sha256(PNG).hexdigest(),
    )
    assert good.status_code == 200, good.text
    assert (candidate / "public/visuals/banner.png").read_bytes() == PNG + b"new"


def test_digest_or_magic_mismatch_never_writes(assets):
    client, candidate, _ = assets
    mismatch = _put(client, sha="0" * 64)
    assert mismatch.status_code == 400
    bad = _put(client, payload=b"html<script>alert(1)</script>")
    assert bad.status_code == 400
    assert not (candidate / "public/visuals/banner.png").exists()


def test_restricted_paths_and_symlinks_fail_closed(assets, tmp_path):
    client, candidate, source = assets
    for path in (
        "../outside.png", "public/.env", "public/visuals/evil.html",
        "src/pages/index.png", "public/scripts/evil.png", "/etc/passwd",
    ):
        result = _put(client, path=path)
        assert result.status_code in {400, 403}, (path, result.text)
    outside = tmp_path / "external"
    outside.mkdir()
    (candidate / "public/visuals/link.png").symlink_to(outside / "loot.png")
    result = _put(client, path="public/visuals/link.png")
    assert result.status_code == 400
    direct = client.put(
        "/api/workspace/projects/xloud/assets/upload?path=public/visuals/x.png&sha256="
        + hashlib.sha256(PNG).hexdigest(),
        content=PNG,
        headers={"X-API-Key": "asset-test-key", "Content-Type": "image/png"},
    )
    assert direct.status_code == 400
    assert not (source / "public/visuals/x.png").exists()


def test_content_type_limit_and_readonly(assets, monkeypatch):
    client, candidate, _ = assets
    wrong_type = _put(client, mime="text/html")
    assert wrong_type.status_code == 400
    large = _put(client, payload=PNG + b"x" * (8 * 1024 * 1024))
    assert large.status_code == 413
    monkeypatch.setattr(settings, "workspace_readonly", True)
    blocked = _put(client)
    assert blocked.status_code == 403
    assert not (candidate / "public/visuals/banner.png").exists()


def test_parent_creation_opt_in_and_type_awareness(assets):
    client, candidate, _ = assets
    data = b"wOF2" + b"\x00" * 60
    path = "public/fonts/new/brand.woff2"
    missing = _put(client, data, path=path, mime="font/woff2")
    assert missing.status_code == 400
    written = _put(client, data, path=path, mime="font/woff2", extra="&create_parents=true")
    assert written.status_code == 200, written.text
    assert (candidate / path).read_bytes() == data
    assert written.json()["media_type"] == "font/woff2"

def test_png_dimensions_are_visible_without_decoding_pixels(assets):
    client, _, _ = assets
    header = (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + (1983).to_bytes(4, "big")
        + (793).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
        + b"data"
    )
    result = _put(client, payload=header)
    assert result.status_code == 200, result.text
    assert result.json()["width"] == 1983
    assert result.json()["height"] == 793


def test_readonly_token_cannot_upload_or_download_without_scope(assets, monkeypatch):
    from unittest.mock import AsyncMock

    from app.auth_middleware import AuthIdentity

    client, candidate, _ = assets
    readonly = AuthIdentity(
        token_type="agent", token="readonly", name="readonly",
        scopes=("project:read",),
    )
    with monkeypatch.context() as context:
        context.setattr(
            "app.auth_middleware.verify_api_key",
            AsyncMock(return_value=readonly),
        )
        denied = _put(client)
    assert denied.status_code == 403
    assert not (candidate / "public/visuals/banner.png").exists()


def test_openapi_has_grouped_asset_endpoints_without_new_mcp_tools():
    paths = app.openapi()["paths"]
    expected = {
        "/api/workspace/projects/{project_id}/assets/inspect",
        "/api/workspace/projects/{project_id}/assets/upload",
        "/api/workspace/projects/{project_id}/assets/download",
    }
    assert expected.issubset(paths)
    assert all("workspace-assets" in paths[path][next(iter(paths[path]))]["tags"] for path in expected)