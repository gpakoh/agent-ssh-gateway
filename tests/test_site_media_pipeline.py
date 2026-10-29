"""Isolated media pipeline contracts: rights, hashes, worker evidence, no originals lost."""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from app.config import settings
from app.workspace.assets import AssetConflict, AssetError
from app.workspace.media_pipeline import MediaUnavailable, convert_candidate_image
from app.workspace.models import ProjectInfo
from app.workspace.registry import WorkspaceRegistry

SOURCE = "public/visuals/hero.png"
TARGET = "public/visuals/hero-sharp.webp"
PROJECT = "candidate-xloud-media"
ORIGINAL = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
OUTPUT = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"data" * 20


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeResponse:
    status = 200

    def __init__(self, payload=OUTPUT, *, override=None):
        self.payload = payload
        self.headers = {
            "Content-Type": "image/webp",
            "X-Media-Source-Sha256": digest(ORIGINAL),
            "X-Media-Output-Sha256": digest(payload),
            "X-Media-Width": "1983",
            "X-Media-Height": "793",
            "X-Media-PSNR": "46.2",
        }
        if override:
            self.headers.update(override)

    def read(self, count):
        assert count >= len(self.payload)
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    base = tmp_path / "candidate"
    source = tmp_path / "source"
    for r in (base, source):
        (r / "public/visuals").mkdir(parents=True)
    (base / SOURCE).write_bytes(ORIGINAL)
    projects = {
        PROJECT: ProjectInfo(PROJECT, base, "app", "candidate", []),
        "xloud": ProjectInfo("xloud", source, "app", "source", []),
    }
    registry = WorkspaceRegistry(
        projects, [tmp_path],
        granted_scopes={"project:read", "project:write"},
    )
    monkeypatch.setattr(settings, "workspace_readonly", False)
    monkeypatch.setenv("SITE_MEDIA_WORKER_TOKEN", "X" * 48)
    return registry, base


def _convert(registry, *, source=SOURCE, target=TARGET, source_sha=None, **kwargs):
    return convert_candidate_image(
        registry, PROJECT, source, target,
        source_sha256=source_sha or digest(ORIGINAL),
        codec="webp", **kwargs,
    )


def test_verified_conversion_never_mutates_source(candidate):
    registry, root = candidate
    class FakeOpener:
        def open(self, request, timeout):
            assert timeout <= 90
            assert request.full_url == "http://site-media-worker:8091/v1/convert"
            assert request.data == ORIGINAL
            assert request.headers["X-media-source-sha256"] == digest(ORIGINAL)
            assert request.headers["X-media-codec"] == "webp"
            return FakeResponse()
    with patch("app.workspace.media_pipeline.urllib.request.build_opener", return_value=FakeOpener()) as opener:
        result = _convert(registry)
    assert result["verified"] is True
    assert result["output"]["sha256"] == digest(OUTPUT)
    assert result["psnr"] == 46.2
    assert (root / SOURCE).read_bytes() == ORIGINAL
    assert (root / TARGET).read_bytes() == OUTPUT
    opener.assert_called_once()


def test_source_integrity_fenced_before_call(candidate):
    registry, root = candidate
    with patch("app.workspace.media_pipeline.urllib.request.build_opener") as opener:
        with pytest.raises(AssetConflict, match="Source SHA"):
            _convert(registry, source_sha="f" * 64)
    opener.assert_not_called()
    assert not (root / TARGET).exists()


def test_reject_untrusted_worker_evidence_and_preserve_target(candidate):
    registry, root = candidate
    variations = [
        {"X-Media-Output-Sha256": "0" * 64},
        {"X-Media-Source-Sha256": "0" * 64},
        {"X-Media-PSNR": "24.5"},
        {"Content-Type": "text/html"},
        {"X-Media-Width": "99999"},
    ]
    for change in variations:
        class FakeOpener:
            def __init__(self, override):
                self.override = override

            def open(self, *_args, **_kwargs):
                return FakeResponse(override=self.override)
        with patch("app.workspace.media_pipeline.urllib.request.build_opener", return_value=FakeOpener(change)):
            with pytest.raises(MediaUnavailable):
                _convert(registry)
        assert not (root / TARGET).exists()


def test_existing_target_requires_compare_and_swap(candidate):
    registry, root = candidate
    (root / TARGET).write_bytes(OUTPUT + b"old")
    class FakeOpener:
        def open(self, *_args, **_kwargs):
            return FakeResponse()
    with patch("app.workspace.media_pipeline.urllib.request.build_opener", return_value=FakeOpener()):
        with pytest.raises(AssetConflict):
            _convert(registry)
        with pytest.raises(AssetConflict):
            _convert(registry, previous_sha256="1" * 64)
        done = _convert(registry, previous_sha256=digest(OUTPUT + b"old"))
    assert done["output"]["previous_sha256"] == digest(OUTPUT + b"old")
    assert (root / TARGET).read_bytes() == OUTPUT


def test_closed_without_token_and_unavailable_worker(candidate, monkeypatch):
    registry, root = candidate
    monkeypatch.delenv("SITE_MEDIA_WORKER_TOKEN")
    with pytest.raises(MediaUnavailable, match="not configured"):
        _convert(registry)
    monkeypatch.setenv("SITE_MEDIA_WORKER_TOKEN", "X" * 48)
    class Down:
        def open(self, *_args, **_kwargs):
            raise TimeoutError()
    with patch("app.workspace.media_pipeline.urllib.request.build_opener", return_value=Down()):
        with pytest.raises(MediaUnavailable, match="unavailable"):
            _convert(registry)
    assert not (root / TARGET).exists()


def test_candidate_only_target_validation_and_readonly(candidate, monkeypatch):
    registry, root = candidate
    with pytest.raises(AssetError, match="Target extension"):
        _convert(registry, target="public/visuals/hero.svg")
    with pytest.raises(AssetError, match="Original source"):
        _convert(registry, target=SOURCE, source=SOURCE)
    with pytest.raises(AssetError, match="Only approved"):
        _convert(registry, target="src/pages/hijack.webp")
    with pytest.raises(AssetError, match="max_width"):
        _convert(registry, max_width=100)
    monkeypatch.setattr(settings, "workspace_readonly", True)
    with pytest.raises(AssetError, match="read-only"):
        _convert(registry)
    assert (root / SOURCE).read_bytes() == ORIGINAL


def test_tool_in_sites_group_only_write_mode_and_scope(candidate, monkeypatch):
    from examples.mcp_server.gateway_catalog import group_for
    from examples.mcp_server.mcp_infra.adapters import site_media
    from examples.mcp_server.tool_modes import TOOL_NAMES_BY_MODE, should_register_tool
    from examples.mcp_server.tool_scopes import get_required_scopes
    registry, root = candidate
    monkeypatch.setattr(site_media, "server_attr", lambda _name: lambda: registry)
    assert group_for("site_media_pipeline") == "sites"
    assert get_required_scopes("site_media_pipeline") == ["mcp:project"]
    assert should_register_tool("site_media_pipeline", "mcp_client_write")
    for mode in ("minimal", "standard", "full", "mcp_client"):
        assert "site_media_pipeline" not in TOOL_NAMES_BY_MODE[mode]
    monkeypatch.delenv("SITE_MEDIA_WORKER_TOKEN")
    result = site_media.site_media_pipeline(PROJECT, SOURCE, TARGET, digest(ORIGINAL), codec="webp")
    assert result["ok"] is False
    assert result["error"]["code"] == "MEDIA_WORKER_UNAVAILABLE"
    assert not (root / TARGET).exists()
