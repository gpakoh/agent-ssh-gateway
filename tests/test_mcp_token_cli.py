"""Tests for the mcp-token CLI tool."""

import json
import os
import tempfile

import pytest

from examples.mcp_server.token_store import TokenStore
from scripts.mcp_token_cli import main


@pytest.fixture
def store_path():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    os.environ["MCP_TOKEN_STORE_FILE"] = path
    yield path
    if os.path.exists(path):
        os.unlink(path)
    lock_path = path + ".lock"
    if os.path.exists(lock_path):
        os.unlink(lock_path)
    del os.environ["MCP_TOKEN_STORE_FILE"]


def test_cli_create_output_text(store_path):
    exit_code = main(["create", "my-token"])
    assert exit_code == 0

    store = TokenStore(store_path)
    entries = store.load()
    assert len(entries) == 1
    assert entries[0].name == "my-token"
    assert entries[0].profile == "full"
    assert entries[0].token_hash.startswith("sha256:")
    assert entries[0].expires_at is None


def test_cli_create_with_profile(store_path):
    exit_code = main(["create", "operator-token", "--profile", "operator"])
    assert exit_code == 0

    store = TokenStore(store_path)
    entries = store.load()
    assert len(entries) == 1
    assert entries[0].name == "operator-token"
    assert entries[0].profile == "operator"


def test_cli_create_output_json(store_path, capsys):
    exit_code = main(["create", "json-token", "--output", "json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["name"] == "json-token"
    assert data["token"].startswith("mcp_")
    assert data["token_hash"].startswith("sha256:")
    assert len(data["scopes"]) > 0


def test_cli_list_empty(store_path, capsys):
    exit_code = main(["list"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No tokens" in captured.out


def test_cli_list_with_tokens(store_path, capsys):
    main(["create", "tok-a"])
    main(["create", "tok-b", "--profile", "operator"])
    captured = capsys.readouterr()
    # list command
    main(["list"])
    captured = capsys.readouterr()
    assert "tok-a" in captured.out
    assert "tok-b" in captured.out
    assert "operator" in captured.out


def test_cli_list_json(store_path, capsys):
    main(["create", "json-list"])
    _ = capsys.readouterr()
    main(["list", "--output", "json"])
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 1
    assert data[0]["name"] == "json-list"


def test_cli_revoke(store_path):
    main(["create", "revocable"])
    store = TokenStore(store_path)
    entry = store.load()[0]
    exit_code = main(["revoke", entry.id])
    assert exit_code == 0

    store2 = TokenStore(store_path)
    assert store2.load()[0].revoked_at is not None


def test_cli_revoke_nonexistent(store_path, capsys):
    exit_code = main(["revoke", "nonexistent"])
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "not found" in captured.err.lower()


def test_cli_rotate(store_path):
    main(["create", "rotatable"])
    store = TokenStore(store_path)
    old_entry = store.load()[0]
    old_hash = old_entry.token_hash
    old_id = old_entry.id

    exit_code = main(["rotate", old_id])
    assert exit_code == 0

    store2 = TokenStore(store_path)
    entries = store2.load()
    assert len(entries) == 2
    # old should be revoked
    old = [e for e in entries if e.id == old_id][0]
    assert old.revoked_at is not None
    # new should be active with different hash
    new = [e for e in entries if e.id != old_id][0]
    assert new.token_hash != old_hash
    assert new.revoked_at is None


def test_cli_rotate_json(store_path, capsys):
    main(["create", "rot-json"])
    _ = capsys.readouterr()
    store = TokenStore(store_path)
    old_id = store.load()[0].id

    exit_code = main(["rotate", old_id, "--output", "json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["token"].startswith("mcp_")
    assert data["token_hash"].startswith("sha256:")
