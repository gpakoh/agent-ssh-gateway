# MCP Token Management CLI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add hash-based token storage, persistent token store, and CLI management for MCP tokens.

**Architecture:** `GatewayOAuthProvider` stores tokens keyed by `sha256(raw)` with `"sha256:"` prefix. A `TokenStore` class manages the persistent JSON file at `MCP_TOKEN_STORE_FILE`. A CLI (`mcp-token`) creates, lists, revokes, and rotates tokens. `server.py` uses unified `register_static_token()` / `register_hashed_token()` methods for all token sources.

**Tech Stack:** Python 3.11, hashlib, fcntl, argparse, json, dataclasses, FastMCP

## Global Constraints

- All token hashes use `"sha256:" + sha256(token.encode("utf-8")).hexdigest()` format
- `StoredToken.token` stores the hash, never raw token
- Provider never writes to store file (CLI-only writes)
- Token store file at `MCP_TOKEN_STORE_FILE` (default `/var/lib/agent-ssh-gateway/mcp_tokens.json`)
- Store file permissions: `chmod 600`, parent dir `chmod 700`
- `TokenStore.save()` uses temp file + `os.replace()` + `fcntl.flock()`
- Profiles are hardcoded in `tool_scopes.py` (`ACCESS_PROFILES`)
- Raw token is printed exactly once at `create`
- `MCP_PUBLIC_TOKEN_PROFILE` env var (default `"operator"`)

---
### Task 1: Provider hash infrastructure (`oauth_provider.py` + tests)

**Files:**
- Modify: `examples/mcp_server/oauth_provider.py`
- Modify: `tests/test_oauth_provider.py`

**Interfaces:**
- Consumes: `get_profile_scopes()` from `tool_scopes.py` (already imported via `oauth_provider.py`)
- Produces: `hash_token(token: str) -> str` (module-level), `register_static_token(raw_token, profile, name, client_id="mcp_static")`, `register_hashed_token(token_hash, scopes, profile, name, client_id="mcp_static")`, updated `verify_access_token()`, `load_access_token()`, updated OAuth flow methods

- [ ] **Step 1: Add `hash_token()` helper to `oauth_provider.py`**

Add at module level after imports (before `SUPPORTED_SCOPES`):

```python
def hash_token(token: str) -> str:
    """Return sha256 hash with explicit 'sha256:' prefix."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
```

- [ ] **Step 2: Write tests for `hash_token()` in `test_oauth_provider.py`**

Add after line 14 (imports), before test_pkce...:

```python
from examples.mcp_server.oauth_provider import (
    DEFAULT_SCOPES,
    SUPPORTED_SCOPES,
    GatewayOAuthProvider,
    _generate_code_challenge,
    _parse_scopes,
    _verify_pkce,
    hash_token,  # NEW
)


def test_hash_token_has_prefix():
    h = hash_token("hello")
    assert h.startswith("sha256:")
    assert len(h) == 7 + 64  # "sha256:" + 64 hex chars


def test_hash_token_deterministic():
    assert hash_token("test-token") == hash_token("test-token")


def test_hash_token_differs():
    assert hash_token("token-a") != hash_token("token-b")


def test_hash_token_format():
    h = hash_token("hello")
    hex_part = h[7:]
    assert all(c in "0123456789abcdef" for c in hex_part)
```

- [ ] **Step 3: Run hash_token tests to verify they pass**

Run: `python -m pytest tests/test_oauth_provider.py::test_hash_token_has_prefix tests/test_oauth_provider.py::test_hash_token_deterministic tests/test_oauth_provider.py::test_hash_token_differs tests/test_oauth_provider.py::test_hash_token_format -v`

Expected: 4 PASS

- [ ] **Step 4: Add `register_static_token()` and `register_hashed_token()` methods to `GatewayOAuthProvider`**

Add after `__init__` (after line 116), before `# --- Client Registration ---`:

```python
def register_static_token(
    self,
    raw_token: str,
    profile: str = "operator",
    name: str = "static",
    client_id: str = "mcp_static",
) -> str:
    """Register a raw static token. Returns the hash used as key.

    Hashes the token internally, resolves scopes from profile,
    stores with infinite expiry.
    """
    from examples.mcp_server.tool_scopes import get_profile_scopes

    token_hash = hash_token(raw_token)
    scopes = get_profile_scopes(profile)
    self._tokens[token_hash] = StoredToken(
        token=token_hash,
        client_id=client_id,
        scopes=list(scopes),
        expires_at=float("inf"),
        type="access",
    )
    return token_hash


def register_hashed_token(
    self,
    token_hash: str,
    scopes: list[str],
    profile: str = "operator",
    name: str = "hashed",
    client_id: str = "mcp_static",
) -> None:
    """Register a pre-hashed token (from persistent store).

    Validates the 'sha256:' prefix and stores directly.
    """
    if not token_hash.startswith("sha256:"):
        raise ValueError(f"token_hash must start with 'sha256:', got {token_hash[:20]}...")
    self._tokens[token_hash] = StoredToken(
        token=token_hash,
        client_id=client_id,
        scopes=list(scopes),
        expires_at=float("inf"),
        type="access",
    )
```

- [ ] **Step 5: Write tests for register_static_token and register_hashed_token**

```python
def test_register_static_token_hashes_key():
    provider = GatewayOAuthProvider()
    provider.register_static_token("my-raw-token", profile="full", name="test")
    raw_hash = hash_token("my-raw-token")
    stored = provider._tokens.get(raw_hash)
    assert stored is not None
    assert stored.client_id == "mcp_static"
    assert "mcp:admin" in stored.scopes  # full profile has admin
    assert stored.token == raw_hash  # token field stores hash, not raw


def test_register_static_token_raw_not_in_keys():
    provider = GatewayOAuthProvider()
    provider.register_static_token("secret-42", profile="viewer", name="v")
    assert "secret-42" not in provider._tokens


def test_register_hashed_token():
    provider = GatewayOAuthProvider()
    token_hash = hash_token("some-token")
    provider.register_hashed_token(token_hash, scopes=["mcp:read"], profile="viewer", name="h")
    stored = provider._tokens.get(token_hash)
    assert stored is not None
    assert stored.scopes == ["mcp:read"]


def test_register_hashed_token_rejects_bad_prefix():
    provider = GatewayOAuthProvider()
    with pytest.raises(ValueError, match="must start with 'sha256:'"):
        provider.register_hashed_token("md5:abc", scopes=["mcp:read"], profile="v", name="bad")


def test_register_static_token_with_custom_client_id():
    provider = GatewayOAuthProvider()
    provider.register_static_token("tk", profile="operator", name="op", client_id="mcp_healthcheck")
    h = hash_token("tk")
    assert provider._tokens[h].client_id == "mcp_healthcheck"
```

- [ ] **Step 6: Run tests to verify register methods work**

Run: `python -m pytest tests/test_oauth_provider.py::test_register_static_token_hashes_key tests/test_oauth_provider.py::test_register_static_token_raw_not_in_keys tests/test_oauth_provider.py::test_register_hashed_token tests/test_oauth_provider.py::test_register_hashed_token_rejects_bad_prefix tests/test_oauth_provider.py::test_register_static_token_with_custom_client_id -v`

Expected: 5 PASS

- [ ] **Step 7: Update `verify_access_token()` and `load_access_token()` to use hash lookup**

Replace `verify_access_token` (lines 285-294):

```python
def verify_access_token(self, token_str: str) -> StoredToken | None:
    """Verify and return access token using hash lookup."""
    token_hash = hash_token(token_str)
    stored = self._tokens.get(token_hash)
    if not stored:
        return None
    if stored.type != "access":
        return None
    if time.time() > stored.expires_at:
        return None
    return stored
```

Replace `load_access_token` (lines 296-311):

```python
async def load_access_token(self, token_str: str) -> AccessToken | None:
    """Async token loader for FastMCP ProviderTokenVerifier (hash lookup)."""
    token_hash = hash_token(token_str)
    stored = self._tokens.get(token_hash)
    if not stored:
        return None
    if stored.type != "access":
        return None
    if time.time() > stored.expires_at:
        return None
    expires_at = int(stored.expires_at) if stored.expires_at != float("inf") else 2**63 - 1
    return AccessToken(
        token=token_str,  # pass raw token from request, not stored.token (which is hash)
        client_id=stored.client_id,
        scopes=stored.scopes,
        expires_at=expires_at,
    )
```

- [ ] **Step 8: Update OAuth flow methods to use hash keys**

In `exchange_code_for_token()` (around line 217), replace:

```python
        self._tokens[access_token] = StoredToken(
            token=access_token, client_id=client_id, scopes=stored.scopes,
            expires_at=time.time() + 7200, type="access",
        )
        self._tokens[refresh_token] = StoredToken(
            token=refresh_token, client_id=client_id, scopes=stored.scopes,
            expires_at=time.time() + 604800, type="refresh",
        )
```

With:

```python
        at_hash = hash_token(access_token)
        rt_hash = hash_token(refresh_token)
        self._tokens[at_hash] = StoredToken(
            token=at_hash, client_id=client_id, scopes=stored.scopes,
            expires_at=time.time() + 7200, type="access",
        )
        self._tokens[rt_hash] = StoredToken(
            token=rt_hash, client_id=client_id, scopes=stored.scopes,
            expires_at=time.time() + 604800, type="refresh",
        )
```

In `refresh_access_token()` (around line 241), replace:

```python
        new_access = _generate_id("mcp_at_", 32)
        self._tokens[new_access] = StoredToken(
            token=new_access, client_id=client_id, scopes=stored.scopes,
            expires_at=time.time() + 7200, type="access",
        )
        return {"access_token": new_access, "token_type": "Bearer", "expires_in": 7200, "scope": " ".join(stored.scopes)}
```

With:

```python
        new_access = _generate_id("mcp_at_", 32)
        at_hash = hash_token(new_access)
        self._tokens[at_hash] = StoredToken(
            token=at_hash, client_id=client_id, scopes=stored.scopes,
            expires_at=time.time() + 7200, type="access",
        )
        return {"access_token": new_access, "token_type": "Bearer", "expires_in": 7200, "scope": " ".join(stored.scopes)}
```

In `revoke_client_token()` (line 248-251), replace:

```python
    def revoke_client_token(self, client_id: str, token_str: str) -> None:
        stored = self._tokens.get(token_str)
        if stored and stored.client_id == client_id:
            del self._tokens[token_str]
```

With:

```python
    def revoke_client_token(self, client_id: str, token_str: str) -> None:
        token_hash = hash_token(token_str)
        stored = self._tokens.get(token_hash)
        if stored and stored.client_id == client_id:
            del self._tokens[token_hash]
```

In `revoke_token()` (line 278-281), replace:

```python
    async def revoke_token(self, token_str: str) -> None:
        stored = self._tokens.get(token_str)
        if stored:
            del self._tokens[token_str]
```

With:

```python
    async def revoke_token(self, token_str: str) -> None:
        token_hash = hash_token(token_str)
        stored = self._tokens.get(token_hash)
        if stored:
            del self._tokens[token_hash]
```

- [ ] **Step 9: Run all oauth provider tests to verify nothing is broken**

Run: `python -m pytest tests/test_oauth_provider.py -v`

Expected: all tests pass (existing 17 tests + 9 new = 26 tests). Note: `test_access_token_verification` and `test_revoke_token` should still pass because `verify_access_token()` now does hash lookup, and `exchange_code_for_token()` stores with hash key.

- [ ] **Step 10: Commit Task 1**

```bash
git add examples/mcp_server/oauth_provider.py tests/test_oauth_provider.py
git commit -m "feat: add hash-lookup token storage to GatewayOAuthProvider

- hash_token() helper with 'sha256:' prefix
- register_static_token() / register_hashed_token() unified registration
- verify_access_token() and load_access_token() use hash lookup
- All OAuth flow methods use hash keys internally
- StoredToken.token stores hash, never raw token"
```

---
### Task 2: TokenStore class (`token_store.py` + tests)

**Files:**
- Create: `examples/mcp_server/token_store.py`
- Create: `tests/test_token_store.py`

**Interfaces:**
- Consumes: nothing (standalone)
- Produces: `StoredTokenEntry` dataclass, `TokenStore(path)` with `load()`, `save()`, `add()`, `revoke()`, `find_by_hash()` methods

- [ ] **Step 1: Write tests for TokenStore**

Create `tests/test_token_store.py`:

```python
"""Tests for TokenStore persistence."""
import json
import os
import tempfile

import pytest

from examples.mcp_server.token_store import StoredTokenEntry, TokenStore


@pytest.fixture
def store_path():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.unlink(path)
    lock_path = path + ".lock"
    if os.path.exists(lock_path):
        os.unlink(lock_path)


def test_token_store_create_empty(store_path):
    store = TokenStore(store_path)
    entries = store.load()
    assert entries == []


def test_token_store_add_and_load(store_path):
    store = TokenStore(store_path)
    entry = StoredTokenEntry(
        id="tok_20260626_test",
        token_hash="sha256:abc123",
        name="test-token",
        profile="full",
        scopes=["mcp:read", "mcp:admin"],
        created_at="2026-06-26T12:00:00Z",
        expires_at=None,
        revoked_at=None,
        last_used_at=None,
    )
    store.add(entry)

    # Read from a new instance to verify persistence
    store2 = TokenStore(store_path)
    loaded = store2.load()
    assert len(loaded) == 1
    assert loaded[0].id == "tok_20260626_test"
    assert loaded[0].token_hash == "sha256:abc123"
    assert loaded[0].scopes == ["mcp:read", "mcp:admin"]


def test_token_store_revoke(store_path):
    store = TokenStore(store_path)
    entry = StoredTokenEntry(
        id="tok_revoke_me",
        token_hash="sha256:xyz",
        name="revocable",
        profile="operator",
        scopes=["mcp:read"],
        created_at="2026-06-26T12:00:00Z",
        expires_at=None,
        revoked_at=None,
        last_used_at=None,
    )
    store.add(entry)
    revoked = store.revoke("tok_revoke_me")
    assert revoked is not None
    assert revoked.revoked_at is not None

    store2 = TokenStore(store_path)
    loaded = store2.load()
    assert loaded[0].revoked_at is not None


def test_token_store_revoke_nonexistent(store_path):
    store = TokenStore(store_path)
    assert store.revoke("nonexistent") is None


def test_token_store_find_by_hash(store_path):
    store = TokenStore(store_path)
    store.add(StoredTokenEntry(
        id="tok_find",
        token_hash="sha256:findme",
        name="findable",
        profile="viewer",
        scopes=["mcp:read"],
        created_at="2026-06-26T12:00:00Z",
        expires_at=None,
        revoked_at=None,
        last_used_at=None,
    ))
    found = store.find_by_hash("sha256:findme")
    assert found is not None
    assert found.id == "tok_find"
    assert store.find_by_hash("sha256:nope") is None


def test_token_store_version_in_file(store_path):
    store = TokenStore(store_path)
    store.add(StoredTokenEntry(
        id="tok_v1", token_hash="sha256:v1", name="v1",
        profile="full", scopes=["mcp:read"],
        created_at="2026-06-26T12:00:00Z",
    ))
    with open(store_path) as f:
        data = json.load(f)
    assert data["version"] == 1


def test_token_store_enforces_permissions(store_path):
    # Make store world-writable
    with open(store_path, "w") as f:
        json.dump({"version": 1, "tokens": []}, f)
    os.chmod(store_path, 0o666)
    with pytest.raises(PermissionError, match="world-writable"):
        TokenStore(store_path).load()
```

- [ ] **Step 2: Create `TokenStore` class**

Create `examples/mcp_server/token_store.py`:

```python
"""Persistent token store for MCP token management.

Stores only hashed tokens (never raw tokens). Uses atomic write with
fcntl.flock for concurrency safety. Enforces strict file permissions.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass
class StoredTokenEntry:
    id: str
    token_hash: str
    name: str
    profile: str
    scopes: list[str]
    created_at: str  # ISO 8601
    expires_at: str | None = None
    revoked_at: str | None = None
    last_used_at: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _generate_token_id(name: str) -> str:
    date_part = _now_iso()[:10].replace("-", "")
    safe_name = name.replace(" ", "_").lower()[:20]
    rand_suffix = os.urandom(4).hex()
    return f"tok_{date_part}_{safe_name}_{rand_suffix}"


class TokenStore:
    """Manage persistent token store file.

    Thread-safe for reads (single-process FastMCP). Write-safe via
    fcntl.flock on .lock file for CLI operations.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock_path = path + ".lock"

    def load(self) -> list[StoredTokenEntry]:
        """Load tokens from store file."""
        if not os.path.isfile(self._path):
            return []
        self._check_permissions()
        with open(self._path) as f:
            data = json.load(f)
        return [StoredTokenEntry(**t) for t in data.get("tokens", [])]

    def save(self, entries: list[StoredTokenEntry]) -> None:
        """Save tokens atomically with flock."""
        self._ensure_dir()
        self._ensure_permissions()
        with open(self._lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                data = {
                    "version": 1,
                    "tokens": [asdict(e) for e in entries],
                }
                fd, tmp_path = tempfile.mkstemp(
                    dir=os.path.dirname(self._path),
                    prefix=".mcp_tokens_tmp_",
                )
                try:
                    with os.fdopen(fd, "w") as tmp_f:
                        json.dump(data, tmp_f, indent=2)
                        tmp_f.flush()
                        os.fsync(fd)
                    os.replace(tmp_path, self._path)
                    os.chmod(self._path, 0o600)
                except BaseException:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)

    def add(self, entry: StoredTokenEntry) -> None:
        """Add a single token entry."""
        entries = self.load()
        entries.append(entry)
        self.save(entries)

    def revoke(self, token_id: str) -> StoredTokenEntry | None:
        """Soft-delete a token by setting revoked_at. Returns the entry or None."""
        entries = self.load()
        for e in entries:
            if e.id == token_id:
                e.revoked_at = _now_iso()
                self.save(entries)
                return e
        return None

    def find_by_hash(self, token_hash: str) -> StoredTokenEntry | None:
        """Look up an entry by its token hash."""
        entries = self.load()
        for e in entries:
            if e.token_hash == token_hash:
                return e
        return None

    def _check_permissions(self) -> None:
        """Check that store file is not world/group-writable."""
        mode = os.stat(self._path).st_mode
        if mode & 0o022:  # group or other writable
            raise PermissionError(
                f"Token store is world/group-writable: {self._path} (mode {oct(mode & 0o777)})"
            )

    def _ensure_dir(self) -> None:
        """Ensure parent directory exists with 0700."""
        parent = os.path.dirname(self._path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, mode=0o700, exist_ok=True)

    def _ensure_permissions(self) -> None:
        """Enforce 0600 on store file, 0700 on parent dir."""
        parent = os.path.dirname(self._path)
        if parent:
            os.chmod(parent, 0o700)
        if os.path.isfile(self._path):
            os.chmod(self._path, 0o600)
```

- [ ] **Step 3: Run TokenStore tests**

Run: `python -m pytest tests/test_token_store.py -v`

Expected: 8 PASS

- [ ] **Step 4: Commit Task 2**

```bash
git add examples/mcp_server/token_store.py tests/test_token_store.py
git commit -m "feat: add TokenStore class for persistent MCP token storage

- StoredTokenEntry dataclass with id, token_hash, name, profile, scopes, timestamps
- TokenStore with load, save (atomic), add, revoke, find_by_hash
- fcntl.flock on .lock file for write concurrency
- Permission enforcement: chmod 600 file, 700 parent dir
- Rejects world/group-writable store files"
```

---
### Task 3: CLI tool (`scripts/mcp_token_cli.py`)

**Files:**
- Create: `scripts/mcp_token_cli.py`

**Interfaces:**
- Consumes: `TokenStore`, `StoredTokenEntry`, `hash_token()`, `get_profile_scopes()`
- Produces: `mcp-token` CLI with `create`, `list`, `revoke`, `rotate` commands

- [ ] **Step 1: Create the CLI tool**

Create `scripts/mcp_token_cli.py`:

```python
#!/usr/bin/env python3
"""MCP token management CLI.

Create, list, revoke, and rotate MCP tokens for the agent-ssh-gateway fleet.

Usage:
    mcp-token create --profile full --name private-chatgpt
    mcp-token list
    mcp-token revoke <id>
    mcp-token rotate <id>

Environment:
    MCP_TOKEN_STORE_FILE: path to token store (default: /var/lib/agent-ssh-gateway/mcp_tokens.json)
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys

# Ensure examples/ is on path for imports
_EXAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "examples")
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_EXAMPLES_DIR))

from examples.mcp_server.token_store import StoredTokenEntry, TokenStore, _generate_token_id, _now_iso
from examples.mcp_server.oauth_provider import hash_token
from examples.mcp_server.tool_scopes import ACCESS_PROFILES, get_profile_scopes

DEFAULT_STORE_PATH = "/var/lib/agent-ssh-gateway/mcp_tokens.json"


def _get_store() -> TokenStore:
    path = os.environ.get("MCP_TOKEN_STORE_FILE", DEFAULT_STORE_PATH)
    return TokenStore(path)


def cmd_create(args: argparse.Namespace) -> None:
    """Create a new token, print raw token once."""
    store = _get_store()

    if args.profile not in ACCESS_PROFILES:
        print(f"error: unknown profile '{args.profile}'. Available: {', '.join(ACCESS_PROFILES)}", file=sys.stderr)
        sys.exit(1)

    raw_token = "mcp_" + secrets.token_urlsafe(48)
    token_hash = hash_token(raw_token)
    scopes = get_profile_scopes(args.profile)

    entry = StoredTokenEntry(
        id=_generate_token_id(args.name),
        token_hash=token_hash,
        name=args.name,
        profile=args.profile,
        scopes=scopes,
        created_at=_now_iso(),
    )
    store.add(entry)
    print(raw_token)


def cmd_list(args: argparse.Namespace) -> None:
    """List all tokens."""
    store = _get_store()
    entries = store.load()
    if not entries:
        print("No tokens found.")
        return
    print(f"{'ID':45s} {'NAME':25s} {'PROFILE':15s} {'CREATED':22s} {'REVOKED':22s}")
    print("-" * 130)
    for e in entries:
        revoked = e.revoked_at or "-"
        print(f"{e.id:45s} {e.name:25s} {e.profile:15s} {e.created_at:22s} {revoked:22s}")


def cmd_revoke(args: argparse.Namespace) -> None:
    """Revoke a token by ID."""
    store = _get_store()
    entry = store.revoke(args.token_id)
    if entry is None:
        print(f"error: token '{args.token_id}' not found", file=sys.stderr)
        sys.exit(1)
    print(f"Revoked: {entry.id} ({entry.name})")


def cmd_rotate(args: argparse.Namespace) -> None:
    """Revoke old token, create new one with same name/profile/scopes."""
    store = _get_store()
    entries = store.load()
    old = None
    for e in entries:
        if e.id == args.token_id:
            old = e
            break
    if old is None:
        print(f"error: token '{args.token_id}' not found", file=sys.stderr)
        sys.exit(1)
    if old.revoked_at:
        print(f"error: token '{args.token_id}' is already revoked", file=sys.stderr)
        sys.exit(1)

    # Revoke old
    old.revoked_at = _now_iso()

    # Create new
    raw_token = "mcp_" + secrets.token_urlsafe(48)
    token_hash = hash_token(raw_token)
    new_entry = StoredTokenEntry(
        id=_generate_token_id(old.name),
        token_hash=token_hash,
        name=old.name,
        profile=old.profile,
        scopes=list(old.scopes),
        created_at=_now_iso(),
    )
    entries.append(new_entry)
    store.save(entries)
    print(raw_token)


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP token management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create a new token")
    p_create.add_argument("--profile", required=True, help="Access profile (viewer, operator, agent-runner, infra, full)")
    p_create.add_argument("--name", required=True, help="Human-readable token name")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="List all tokens")
    p_list.set_defaults(func=cmd_list)

    p_revoke = sub.add_parser("revoke", help="Revoke a token by ID")
    p_revoke.add_argument("token_id", help="Token ID to revoke")
    p_revoke.set_defaults(func=cmd_revoke)

    p_rotate = sub.add_parser("rotate", help="Revoke old token and create a new one")
    p_rotate.add_argument("token_id", help="Token ID to rotate")
    p_rotate.set_defaults(func=cmd_rotate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make CLI executable and verify it works**

```bash
chmod +x scripts/mcp_token_cli.py
python scripts/mcp_token_cli.py --help
```

Expected: shows help with create, list, revoke, rotate.

```bash
python scripts/mcp_token_cli.py create --profile viewer --name test-viewer
```

Expected: prints one line — the raw token (starts with `mcp_`).

- [ ] **Step 3: Commit Task 3**

```bash
git add scripts/mcp_token_cli.py
git commit -m "feat: add mcp-token CLI for token management

- create: generates token, stores hash, prints raw once
- list: shows id, name, profile, created, revoked
- revoke: soft-delete by id (sets revoked_at)
- rotate: revoke old + create new with same name/profile"
```

---
### Task 4: Server migration (`server.py` + tests)

**Files:**
- Modify: `examples/mcp_server/server.py`
- Modify: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `register_static_token()`, `register_hashed_token()`, `TokenStore`, `hash_token()`
- Produces: migrated `server.py` startup with unified registration + store file loading

- [ ] **Step 1: Update `server.py` to use `register_static_token()` and `register_hashed_token()`**

Replace the healthcheck token registration block (lines 100-110):

```python
    _health_token = os.environ.get("MCP_HEALTHCHECK_BEARER_TOKEN", "")
    if _health_token:
        _auth_provider.register_static_token(
            raw_token=_health_token,
            profile="full",
            name="healthcheck",
            client_id="mcp_healthcheck",
        )
```

Replace the extra tokens registration block (lines 139-154):

```python
    if _extra_tokens_all:
        for _token_str, _profile in _extra_tokens_all.items():
            _auth_provider.register_static_token(
                raw_token=_token_str,
                profile=_profile,
                name=f"extra_{_profile}",
                client_id=f"mcp_extras_{_profile}",
            )
        print(f"  extra tokens: {len(_extra_tokens_all)} registered", file=sys.stderr)
        if _extra_tokens_file:
            print(f"  extra file  : {_extra_tokens_file}", file=sys.stderr)
```

Replace the `MCP_PUBLIC_TOKEN` registration in token mode (lines 180-188):

```python
    if MCP_AUTH_MODE == "token":
        _auth_provider = GatewayOAuthProvider()
        mcp_token = os.environ.get("MCP_PUBLIC_TOKEN", "")
        if not mcp_token:
            raise ValueError("MCP_PUBLIC_TOKEN is required in token mode")
        _pub_profile = os.environ.get("MCP_PUBLIC_TOKEN_PROFILE", "operator")
        _auth_provider.register_static_token(
            raw_token=mcp_token,
            profile=_pub_profile,
            name="mcp_public",
            client_id="mcp_static_client",
        )
```

Note: the original code has `elif MCP_AUTH_MODE == "token":` — keep that structure, just change the body.

- [ ] **Step 2: Add store file loading to oauth mode startup**

After the extra tokens file block (after line 154, before the AuthSettings try block), add:

```python
    # Load persistent token store
    _store_path = os.environ.get("MCP_TOKEN_STORE_FILE", "")
    if _store_path and not MCP_AUTH_MODE == "token":
        from examples.mcp_server.token_store import TokenStore as _TokenStore

        _token_store = _TokenStore(_store_path)
        _stored_entries = _token_store.load()
        _loaded_count = 0
        for _entry in _stored_entries:
            if _entry.revoked_at:
                continue
            _auth_provider.register_hashed_token(
                token_hash=_entry.token_hash,
                scopes=list(_entry.scopes),
                profile=_entry.profile,
                name=_entry.name,
                client_id=f"mcp_store_{_entry.profile}",
            )
            _loaded_count += 1
        if _loaded_count:
            print(f"  store tokens: {_loaded_count} loaded from {_store_path}", file=sys.stderr)
```

Place this after the extra tokens block (after `print(f"  extra file  : {_extra_tokens_file}", file=sys.stderr)`) and before the AuthSettings try block.

- [ ] **Step 3: Update server tests**

In `tests/test_mcp_server.py`, `test_token_mode_initializes_provider` (line 37-48) should still pass because `verify_access_token("test-token")` now does hash lookup and `register_static_token` hashes internally.

Add a new test for `MCP_PUBLIC_TOKEN_PROFILE`:

```python
@patch.dict(os.environ, {
    "MCP_AUTH_MODE": "token",
    "MCP_PUBLIC_TOKEN": "profile-test-token",
    "MCP_PUBLIC_TOKEN_PROFILE": "full",
})
def test_token_mode_custom_profile():
    """Token mode respects MCP_PUBLIC_TOKEN_PROFILE env var."""
    import importlib
    import examples.mcp_server.server as srv
    importlib.reload(srv)
    token = srv._auth_provider.verify_access_token("profile-test-token")
    assert token is not None
    assert "mcp:admin" in token.scopes  # full profile
```

Add a test for store file loading (set up a temp store file, load it via env var):

```python
@patch.dict(os.environ, {
    "MCP_AUTH_MODE": "oauth",
    "MCP_TOKEN_STORE_FILE": "/tmp/test_mcp_store.json",
})
def test_oauth_mode_loads_store_file():
    """OAuth mode loads tokens from MCP_TOKEN_STORE_FILE."""
    import json
    import importlib

    from examples.mcp_server.oauth_provider import hash_token

    # Create a test store file
    raw = "store-loaded-token"
    store_entry = {
        "id": "tok_test_store",
        "token_hash": hash_token(raw),
        "name": "store-test",
        "profile": "viewer",
        "scopes": ["mcp:read"],
        "created_at": "2026-06-26T12:00:00Z",
        "expires_at": None,
        "revoked_at": None,
        "last_used_at": None,
    }
    with open("/tmp/test_mcp_store.json", "w") as f:
        json.dump({"version": 1, "tokens": [store_entry]}, f)

    try:
        import examples.mcp_server.server as srv
        importlib.reload(srv)
        token = srv._auth_provider.verify_access_token(raw)
        assert token is not None
        assert token.client_id.startswith("mcp_store_")
        assert token.scopes == ["mcp:read"]
    finally:
        import os
        if os.path.exists("/tmp/test_mcp_store.json"):
            os.unlink("/tmp/test_mcp_store.json")
```

- [ ] **Step 4: Run all MCP server tests**

Run: `python -m pytest tests/test_mcp_server.py -v`

Expected: existing tests pass + new tests pass. Note: `test_token_mode_initializes_provider` must still pass since `register_static_token()` hashes internally and `verify_access_token()` looks up by hash.

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/test_oauth_provider.py tests/test_token_store.py tests/test_mcp_server.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit Task 4**

```bash
git add examples/mcp_server/server.py tests/test_mcp_server.py
git commit -m "feat: migrate server.py to hash-lookup token registration

- server.py uses register_static_token() for healthcheck, extras, and public token
- Adds MCP_TOKEN_STORE_FILE loading via register_hashed_token() in oauth mode
- Adds MCP_PUBLIC_TOKEN_PROFILE env var support for token mode
- All token sources go through unified registration API"
```

---
### Task 5: Environment example update

**Files:**
- Modify: `examples/mcp_server/.env.example`

- [ ] **Step 1: Add new env vars to `.env.example`**

Read the current file first, then add:

```bash
# MCP Token Store
# MCP_TOKEN_STORE_FILE=/var/lib/agent-ssh-gateway/mcp_tokens.json

# Token mode profile (default: operator)
# MCP_PUBLIC_TOKEN_PROFILE=operator
```

- [ ] **Step 2: Commit Task 5**

```bash
git add examples/mcp_server/.env.example
git commit -m "chore: add MCP_TOKEN_STORE_FILE and MCP_PUBLIC_TOKEN_PROFILE to .env.example"
```
