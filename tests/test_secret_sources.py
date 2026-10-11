from __future__ import annotations

import os
from pathlib import Path

import pytest

from examples.mcp_server import secret_sources


def _write_source(root: Path, content: bytes = b"token-value\n", mode: int = 0o600) -> Path:
    root.mkdir()
    path = root / "registry_token"
    path.write_bytes(content)
    path.chmod(mode)
    return path


def test_read_approved_secret_source_returns_only_normalized_value_and_hash(tmp_path, monkeypatch):
    root = tmp_path / "sources"
    _write_source(root)
    monkeypatch.setattr(secret_sources, "_SECRET_SOURCE_ROOT", root)

    source = secret_sources.read_approved_secret_source("registry_token")

    assert source.source_id == "registry_token"
    assert source.value == "token-value"
    assert len(source.sha256) == 64
    assert source.sha256 != source.value


def test_read_approved_secret_source_rejects_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_sources, "_SECRET_SOURCE_ROOT", tmp_path)
    with pytest.raises(secret_sources.SecretSourceError, match="not approved"):
        secret_sources.read_approved_secret_source("arbitrary_path")


def test_read_approved_secret_source_rejects_broad_permissions(tmp_path, monkeypatch):
    root = tmp_path / "sources"
    _write_source(root, mode=0o644)
    monkeypatch.setattr(secret_sources, "_SECRET_SOURCE_ROOT", root)
    with pytest.raises(secret_sources.SecretSourceError, match="permissions are too broad"):
        secret_sources.read_approved_secret_source("registry_token")


def test_read_approved_secret_source_rejects_symlink(tmp_path, monkeypatch):
    root = tmp_path / "sources"
    root.mkdir()
    target = tmp_path / "real-token"
    target.write_text("token-value", encoding="utf-8")
    target.chmod(0o600)
    os.symlink(target, root / "registry_token")
    monkeypatch.setattr(secret_sources, "_SECRET_SOURCE_ROOT", root)
    with pytest.raises(secret_sources.SecretSourceError, match="unavailable"):
        secret_sources.read_approved_secret_source("registry_token")


def test_read_approved_secret_source_rejects_control_characters(tmp_path, monkeypatch):
    root = tmp_path / "sources"
    _write_source(root, content=b"token\x00value")
    monkeypatch.setattr(secret_sources, "_SECRET_SOURCE_ROOT", root)
    with pytest.raises(secret_sources.SecretSourceError, match="control characters"):
        secret_sources.read_approved_secret_source("registry_token")
