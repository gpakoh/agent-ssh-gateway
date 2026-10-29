"""MCP site asset lifecycle: actual chunk payloads, CAS and registration."""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

import pytest

from app.config import settings
from app.workspace.asset_transfer import (
    CHUNK_BYTES,
    MAX_SESSIONS,
    UPLOAD_TTL_SECONDS,
    AssetTransferStore,
)
from app.workspace.assets import AssetConflict, AssetError
from app.workspace.models import ProjectInfo
from app.workspace.registry import WorkspaceRegistry

EXAMPLES = str(Path(__file__).resolve().parents[1] / "examples")
MCP_ROOT = str(Path(EXAMPLES) / "mcp_server")
for item in (EXAMPLES, MCP_ROOT):
    if item not in sys.path:
        sys.path.insert(0, item)

from examples.mcp_server.mcp_infra.adapters import site_assets  # noqa: E402

PROJECT = "candidate-xloud-art-20261029"
PATH = "public/visuals/hero.png"
HEADER = (
    b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
    + (1983).to_bytes(4, "big") + (793).to_bytes(4, "big")
    + b"\x08\x06\x00\x00\x00"
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    project = tmp_path / PROJECT
    source = tmp_path / "xloud"
    project.mkdir()
    source.mkdir()
    (project / "public" / "visuals").mkdir(parents=True)
    (source / "public" / "visuals").mkdir(parents=True)
    registry = WorkspaceRegistry(
        {
            PROJECT: ProjectInfo(PROJECT, project, "app", "candidate", []),
            "xloud": ProjectInfo("xloud", source, "app", "source", []),
        },
        [tmp_path],
        granted_scopes={"project:read", "project:write"},
    )
    monkeypatch.setattr(settings, "workspace_readonly", False)
    return registry, project, source


def _part(data: bytes) -> tuple[str, str]:
    return base64.b64encode(data).decode("ascii"), sha(data)


def test_4mb_banner_round_trip_without_text_reencoding(workspace):
    registry, root, _ = workspace
    data = HEADER + b"\x00" * (4 * 1024 * 1024 - len(HEADER))
    store = AssetTransferStore()
    begun = store.begin(
        registry, PROJECT, PATH, sha(data), len(data),
        previous_sha256=None, create_parents=False,
    )
    upload_id = begun["upload_id"]
    assert not (root / PATH).exists(), "begin must not touch the Git checkout"
    for at in range(0, len(data), CHUNK_BYTES):
        piece, checksum = _part(data[at : at + CHUNK_BYTES])
        receipt = store.chunk(upload_id, PROJECT, PATH, at, piece, checksum)
        assert receipt["next_offset"] == min(at + CHUNK_BYTES, len(data))
    assert store.status(upload_id, PROJECT, PATH)["next_offset"] == len(data)
    uploaded = store.finish(registry, upload_id, PROJECT, PATH)
    assert uploaded["sha256"] == sha(data)
    assert uploaded["size"] == len(data)
    assert uploaded["width"] == 1983
    assert (root / PATH).read_bytes() == data
    retrieved = bytearray()
    offset = 0
    while True:
        result = store.read(registry, PROJECT, PATH, offset, sha(data))
        chunk = base64.b64decode(result["chunk_b64"])
        assert sha(chunk) == result["chunk_sha256"]
        retrieved.extend(chunk)
        offset = result["next_offset"]
        if result["eof"]:
            break
    assert bytes(retrieved) == data
    with pytest.raises(AssetError, match="not found"):
        store.status(upload_id, PROJECT, PATH)


def test_replayed_chunk_is_idempotent_and_conflict_is_fail_closed(workspace):
    registry, root, _ = workspace
    data = HEADER + b"x" * 300
    store = AssetTransferStore()
    session = store.begin(registry, PROJECT, PATH, sha(data), len(data),
                          previous_sha256=None, create_parents=False)["upload_id"]
    first, digest = _part(data[:100])
    assert store.chunk(session, PROJECT, PATH, 0, first, digest)["next_offset"] == 100
    replay = store.chunk(session, PROJECT, PATH, 0, first, digest)
    assert replay["replayed"] is True
    with pytest.raises(AssetConflict, match="next_offset=100"):
        store.chunk(session, PROJECT, PATH, 50, *_part(b"different"))
    with pytest.raises(AssetError, match="mismatch"):
        store.chunk(session, PROJECT, PATH, 100, *_part(data[100:])[0:1], "0" * 64)
    assert store.status(session, PROJECT, PATH)["next_offset"] == 100
    with pytest.raises(AssetConflict, match="incomplete"):
        store.finish(registry, session, PROJECT, PATH)
    second, digest = _part(data[100:])
    store.chunk(session, PROJECT, PATH, 100, second, digest)
    store.finish(registry, session, PROJECT, PATH)
    assert (root / PATH).read_bytes() == data


def test_replacement_stale_sha_no_overwrite(workspace):
    registry, root, _ = workspace
    existing = HEADER + b"OLD"
    modified = HEADER + b"NEW"
    (root / PATH).write_bytes(existing)
    store = AssetTransferStore()
    session = store.begin(registry, PROJECT, PATH, sha(modified), len(modified),
                          previous_sha256="0" * 64, create_parents=False)["upload_id"]
    store.chunk(session, PROJECT, PATH, 0, *_part(modified))
    with pytest.raises(AssetConflict, match="changed"):
        store.finish(registry, session, PROJECT, PATH)
    assert (root / PATH).read_bytes() == existing
    store.abort(session, PROJECT, PATH)
    session = store.begin(registry, PROJECT, PATH, sha(modified), len(modified),
                          previous_sha256=sha(existing), create_parents=False)["upload_id"]
    store.chunk(session, PROJECT, PATH, 0, *_part(modified))
    store.finish(registry, session, PROJECT, PATH)
    assert (root / PATH).read_bytes() == modified
    with pytest.raises(AssetConflict, match="changed"):
        store.read(registry, PROJECT, PATH, 0, sha(existing))


def test_rejects_source_path_symlink_and_cross_project_token(workspace, tmp_path):
    registry, root, source = workspace
    store = AssetTransferStore()
    data = HEADER
    with pytest.raises(AssetError, match="candidate"):
        store.begin(registry, "xloud", PATH, sha(data), len(data),
                    previous_sha256=None, create_parents=False)
    with pytest.raises(AssetError, match="Only approved"):
        store.begin(registry, PROJECT, "src/pages/evil.png", sha(data), len(data),
                    previous_sha256=None, create_parents=False)
    outside = tmp_path / "bad.png"
    (root / PATH).symlink_to(outside)
    with pytest.raises(Exception, match="[Ss]ymlink"):
        store.begin(registry, PROJECT, PATH, sha(data), len(data),
                    previous_sha256=None, create_parents=False)
    (root / PATH).unlink()
    session = store.begin(registry, PROJECT, PATH, sha(data), len(data),
                          previous_sha256=None, create_parents=False)["upload_id"]
    with pytest.raises(AssetError, match="does not match"):
        store.chunk(session, "xloud", PATH, 0, *_part(data))
    with pytest.raises(AssetError, match="does not match"):
        store.abort(session, PROJECT, "public/visuals/else.png")
    assert not (source / PATH).exists()
    store.abort(session, PROJECT, PATH)


def test_invalid_base64_offsets_and_excess_limits(workspace):
    registry, root, _ = workspace
    store = AssetTransferStore()
    data = HEADER
    session = store.begin(registry, PROJECT, PATH, sha(data), len(data),
                          previous_sha256=None, create_parents=False)["upload_id"]
    with pytest.raises(AssetError, match="base64"):
        store.chunk(session, PROJECT, PATH, 0, "$%%", sha(data))
    with pytest.raises(AssetError, match="64 KiB"):
        store.chunk(session, PROJECT, PATH, 0, "a" * 100_000, sha(data))
    with pytest.raises(AssetConflict, match="next_offset=0"):
        store.chunk(session, PROJECT, PATH, 1, *_part(data))
    with pytest.raises(AssetError, match="size"):
        store.chunk(session, PROJECT, PATH, 0, *_part(data + b"TOO LARGE"))
    assert not (root / PATH).exists()
    store.abort(session, PROJECT, PATH)


def test_capacity_expiry_and_global_readonly(workspace, monkeypatch):
    registry, root, _ = workspace
    store = AssetTransferStore()
    ids = [store.begin(registry, PROJECT, PATH, sha(HEADER), len(HEADER),
                       previous_sha256=None, create_parents=False)["upload_id"]
           for _ in range(MAX_SESSIONS)]
    with pytest.raises(AssetError, match="Too many"):
        store.begin(registry, PROJECT, PATH, sha(HEADER), len(HEADER),
                    previous_sha256=None, create_parents=False)
    store._uploads[ids[0]].updated -= UPLOAD_TTL_SECONDS + 1
    with pytest.raises(AssetError, match="expired"):
        store.status(ids[0], PROJECT, PATH)
    monkeypatch.setattr(settings, "workspace_readonly", True)
    with pytest.raises(AssetError, match="read-only"):
        store.begin(registry, PROJECT, PATH, sha(HEADER), len(HEADER),
                    previous_sha256=None, create_parents=False)
    with pytest.raises(AssetError, match="read-only"):
        store.chunk(ids[1], PROJECT, PATH, 0, *_part(HEADER))
    with pytest.raises(AssetError, match="read-only"):
        store.finish(registry, ids[1], PROJECT, PATH)
    assert not (root / PATH).exists()


def test_mcp_adapter_envelope_and_tool_visibility(workspace, monkeypatch):
    registry, _, _ = workspace
    monkeypatch.setattr(site_assets, "server_attr", lambda key: lambda: registry)
    monkeypatch.setattr(site_assets, "_STORE", AssetTransferStore())
    start = site_assets.site_asset_transfer(
        "begin", PROJECT, PATH, sha256=sha(HEADER), total_bytes=len(HEADER)
    )
    assert start["ok"], start
    upload_id = start["result"]["upload_id"]
    encoded, part_sha = _part(HEADER)
    chunk = site_assets.site_asset_transfer(
        "chunk", PROJECT, PATH, upload_id=upload_id,
        chunk_b64=encoded, chunk_sha256=part_sha,
    )
    assert chunk["ok"], chunk
    finished = site_assets.site_asset_transfer(
        "finish", PROJECT, PATH, upload_id=upload_id
    )
    assert finished["ok"], finished
    from examples.mcp_server.gateway_catalog import group_for
    from examples.mcp_server.tool_modes import (
        TOOL_NAMES_BY_MODE,
        should_register_tool,
    )
    from examples.mcp_server.tool_scopes import get_required_scopes

    assert group_for("site_asset_transfer") == "sites"
    assert get_required_scopes("site_asset_transfer") == ["mcp:project"]
    assert should_register_tool("site_asset_transfer", "mcp_client_write")
    for mode in ("minimal", "standard", "full", "mcp_client"):
        assert "site_asset_transfer" not in TOOL_NAMES_BY_MODE[mode], mode
    assert site_assets.site_asset_transfer("finish", PROJECT, PATH, upload_id=upload_id)["ok"] is False
