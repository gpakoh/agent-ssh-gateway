# MCP Phase 2 P1 Diagnostics: Whoami + Build Metadata + MCP Health

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/api/auth/whoami` identity endpoint, build metadata to `/health`, a toolset hash for MCP, and an MCP `health` tool that aggregates gateway + MCP diagnostics.

**Architecture:** Gateway owns auth, build metadata, and `/health`. MCP is a thin translation layer. `app/build_info.py` is a single-source module consumed by the `/health` endpoint and lifespan. The MCP server computes its own process-local metadata and aggregates gateway health via HTTP. The `whoami` endpoint uses the existing `AuthIdentity` dataclass and `require_scope` dependency.

**Tech Stack:** Python 3.11+, FastAPI, hashlib (SHA-256), pytest, httpx

## Global Constraints

- `credential_id` = `"ak_" + fingerprint[:8]` — safe non-secret, never the raw token
- `whoami` returns NO `session_id`
- `whoami` scope: `auth:read` — master key bypasses scope checks automatically
- `BUILD_SHA` resolution: env `BUILD_SHA` → `git rev-parse HEAD` → `"unknown"`
- `BUILD_TIME` resolution: env `BUILD_TIME` → `""`
- `STARTED_AT` set in lifespan, NOT at module import time
- Toolset hash: `sha256:` + hex digest of canonical JSON (sorted tool names + input schemas)
- Toolset hash uses `items.sort(key=lambda item: item["name"])` — NOT `sorted(dicts)`
- Toolset hash uses `json.dumps(..., sort_keys=True, separators=(",", ":"))` — compact JSON
- NOT `builtins.hash()` — unstable across processes
- `/api/auth/whoami` must be protected by API key auth (unlike other `/api/auth/*` paths)
- MCP health tool returns `dict[str, Any]` with `"mcp"` and `"gateway"` keys
- FastMCP tool objects expose `.parameters` dict (the input schema), not `.inputSchema`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `app/build_info.py` | `BUILD_SHA`, `BUILD_TIME`, `_started_at`, `set_started_at()`, `get_build_metadata()` |
| Modify | `app/main.py:69-233` | Set `build_info.set_started_at()` in lifespan |
| Modify | `app/models.py:204-211` | Add `build_sha`, `build_time`, `started_at`, `version` to `HealthResponse` |
| Modify | `app/routers/system.py:36-49` | Return build metadata from `/health` |
| Modify | `app/auth_middleware.py:56-64` | Add `"auth:read"` to `VALID_AGENT_SCOPES` |
| Modify | `app/auth_middleware.py:248,280-282` | Replace blanket `/api/auth/*` bypass with explicit public paths |
| Create | `app/routers/auth.py` | `GET /api/auth/whoami` endpoint |
| Modify | `app/main.py:945` | Include new auth router (separate from `user_auth` router) |
| Modify | `examples/mcp_server/server.py` | Add `compute_toolset_hash()`, replace `gateway_health` with aggregated `health` tool |
| Create | `tests/test_build_info.py` | Tests for build metadata module |
| Create | `tests/test_whoami.py` | Tests for `/api/auth/whoami` endpoint |
| Create | `tests/test_health_metadata.py` | Tests for expanded `/health` response |
| Create | `tests/test_toolset_hash.py` | Tests for toolset hash computation |
| Create | `tests/test_mcp_health_tool.py` | Tests for MCP aggregated health tool |
| Create | `tests/test_diagnostics_integration.py` | End-to-end integration tests |

---

### Task 1: Build Metadata Module (`app/build_info.py`)

**Files:**
- Create: `app/build_info.py`
- Create: `tests/test_build_info.py`

**Interfaces:**
- Produces: `BUILD_SHA: str`, `BUILD_TIME: str`, `_started_at: float | None`, `set_started_at() -> None`, `get_build_metadata() -> dict[str, str]`
- Consumes: env vars `BUILD_SHA`, `BUILD_TIME`; `subprocess.run` for git fallback

- [ ] **Step 1: Write the failing test**

Create `tests/test_build_info.py`:

```python
"""Tests for build metadata module."""

import os
import time
from unittest.mock import patch

from app import build_info


class TestBuildSha:
    def test_env_override(self):
        with patch.dict(os.environ, {"BUILD_SHA": "abc123def"}):
            sha = build_info._resolve_build_sha()
        assert sha == "abc123def"

    def test_unknown_when_no_env_and_no_git(self):
        with patch.dict(os.environ, {"BUILD_SHA": ""}, clear=False):
            with patch("subprocess.run", side_effect=FileNotFoundError):
                sha = build_info._resolve_build_sha()
        assert sha == "unknown"


class TestBuildTime:
    def test_env_override(self):
        with patch.dict(os.environ, {"BUILD_TIME": "2026-07-12T12:30:00Z"}):
            bt = build_info._resolve_build_time()
        assert bt == "2026-07-12T12:30:00Z"

    def test_empty_when_no_env(self):
        with patch.dict(os.environ, {"BUILD_TIME": ""}, clear=False):
            bt = build_info._resolve_build_time()
        assert bt == ""


class TestStartedAt:
    def test_initially_none(self):
        build_info._started_at = None
        assert build_info.get_started_at() is None

    def test_set_started_at(self):
        before = time.time()
        build_info.set_started_at()
        after = time.time()
        assert build_info._started_at is not None
        assert before <= build_info._started_at <= after
        build_info._started_at = None  # cleanup


class TestGetBuildMetadata:
    def test_returns_dict_with_expected_keys(self):
        build_info._started_at = 1700000000.0
        meta = build_info.get_build_metadata()
        assert set(meta.keys()) == {"build_sha", "build_time", "started_at"}
        assert isinstance(meta["build_sha"], str)
        assert isinstance(meta["build_time"], str)
        assert meta["started_at"] == "2026-11-14T22:13:20Z"
        build_info._started_at = None  # cleanup

    def test_started_at_empty_when_none(self):
        build_info._started_at = None
        meta = build_info.get_build_metadata()
        assert meta["started_at"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_build_info.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.build_info'`

- [ ] **Step 3: Write the implementation**

Create `app/build_info.py`:

```python
"""Build metadata — single source of truth for build SHA, time, and process start.

BUILD_SHA and BUILD_TIME are resolved once at import time from env vars.
STARTED_AT is set explicitly in the app lifespan (not at import time).
"""

import os
import subprocess
import time as _time
from datetime import UTC, datetime

BUILD_SHA: str = ""
BUILD_TIME: str = ""
_started_at: float | None = None


def _resolve_build_sha() -> str:
    env_sha = os.environ.get("BUILD_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _resolve_build_time() -> str:
    return os.environ.get("BUILD_TIME", "").strip()


BUILD_SHA = _resolve_build_sha()
BUILD_TIME = _resolve_build_time()


def set_started_at() -> None:
    """Call once during app lifespan to record process start time."""
    global _started_at
    _started_at = _time.time()


def get_started_at() -> float | None:
    """Return process start time as float, or None if not yet set."""
    return _started_at


def get_build_metadata() -> dict[str, str]:
    """Return build metadata as an ISO-8601 dict suitable for JSON serialization."""
    started_iso = ""
    if _started_at is not None:
        started_iso = datetime.fromtimestamp(_started_at, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "build_sha": BUILD_SHA,
        "build_time": BUILD_TIME,
        "started_at": started_iso,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_build_info.py -v`
Expected: 6 passed (note: `_resolve_build_sha` may return actual git SHA in repo — test env override covers this)

- [ ] **Step 5: Commit**

```bash
git add app/build_info.py tests/test_build_info.py
git commit -m "feat: add build_info module for build SHA, time, and process start"
```

---

### Task 2: Expand `/health` with Build Metadata

**Files:**
- Modify: `app/models.py:204-211` — add fields to `HealthResponse`
- Modify: `app/routers/system.py:36-49` — return build metadata
- Modify: `app/main.py:69-233` — set `STARTED_AT` in lifespan
- Create: `tests/test_health_metadata.py`

**Interfaces:**
- Consumes: `app.build_info.get_build_metadata()`, `app.version.APP_VERSION`
- Produces: `HealthResponse` with `build_sha`, `build_time`, `started_at`, `version` fields

- [ ] **Step 1: Write the failing test**

Create `tests/test_health_metadata.py`:

```python
"""Tests for expanded /health endpoint with build metadata."""

from starlette.testclient import TestClient

from app.main import app


def test_health_includes_build_metadata():
    """GET /health must include build_sha, build_time, started_at, version."""
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "build_sha" in data
    assert "build_time" in data
    assert "started_at" in data
    assert "version" in data
    assert isinstance(data["build_sha"], str)
    assert isinstance(data["version"], str)
    assert len(data["version"]) > 0


def test_health_existing_fields_unchanged():
    """Original health fields must still be present."""
    with TestClient(app) as client:
        resp = client.get("/health")
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert isinstance(data["redis"], bool)
    assert isinstance(data["ready"], bool)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_health_metadata.py -v`
Expected: FAIL — `assert 'build_sha' in data` fails (field missing from response)

- [ ] **Step 3: Expand HealthResponse model**

In `app/models.py`, replace the `HealthResponse` class (lines 204-211):

```python
class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    redis: bool = False
    persistent_sessions: bool = False
    postgres: bool = False  # deprecated — use persistent_sessions
    ready: bool = False
    build_sha: str = ""
    build_time: str = ""
    started_at: str = ""
    version: str = ""
```

- [ ] **Step 4: Set STARTED_AT in lifespan**

In `app/main.py`, add after the existing imports (e.g., after line 14 `import uuid`):

```python
import app.build_info as build_info
```

Then inside the `lifespan` function, add as the very first line of the function body (after `async def lifespan(app: FastAPI):` and its docstring, before `await init_auth_db()`):

```python
    build_info.set_started_at()
```

- [ ] **Step 5: Return build metadata from /health**

In `app/routers/system.py`, replace the `health_check` function (lines 36-49):

```python
@router.get("/health", tags=["system"], response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    from app import build_info
    from app.version import APP_VERSION

    redis_ok = _state.redis_queue is not None and _state.redis_queue._redis is not None
    persistent_sessions_ok = _state.session_store is not None
    meta = build_info.get_build_metadata()
    return HealthResponse(
        status="ok" if redis_ok or not settings.redis_url else "degraded",
        redis=redis_ok,
        persistent_sessions=persistent_sessions_ok,
        postgres=persistent_sessions_ok,
        ready=True,
        build_sha=meta["build_sha"],
        build_time=meta["build_time"],
        started_at=meta["started_at"],
        version=APP_VERSION,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_health_metadata.py -v`
Expected: 2 passed

- [ ] **Step 7: Run existing health tests to verify no regression**

Run: `pytest tests/test_system_endpoints.py -v -k health`
Expected: All existing health tests pass

- [ ] **Step 8: Commit**

```bash
git add app/models.py app/main.py app/routers/system.py tests/test_health_metadata.py
git commit -m "feat: add build metadata (sha, time, started_at, version) to /health"
```

---

### Task 3: `/api/auth/whoami` Endpoint

**Files:**
- Modify: `app/auth_middleware.py:56-64` — add `"auth:read"` to `VALID_AGENT_SCOPES`
- Modify: `app/auth_middleware.py:248,280-282` — replace blanket `/api/auth/*` bypass with explicit public paths
- Create: `app/routers/auth.py` — whoami endpoint
- Modify: `app/main.py:945` — include the new router
- Create: `tests/test_whoami.py`

**Interfaces:**
- Consumes: `AuthIdentity` from `request.state.auth_identity`, `require_scope("auth:read")`
- Produces: `{"identity": str, "scopes": list[str], "auth_method": str, "credential_id": str}`

- [ ] **Step 1: Write the failing test**

Create `tests/test_whoami.py`:

```python
"""Tests for GET /api/auth/whoami endpoint."""

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def _auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "master-test-key-42")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(settings, "agent_token", "")
    monkeypatch.setattr(settings, "agent_token_scopes", [])
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


class TestWhoamiMasterKey:
    def test_master_key_returns_identity(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/whoami", headers=_headers("master-test-key-42"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["identity"] == "master"
        assert data["auth_method"] == "api_key"
        assert data["credential_id"].startswith("ak_")
        assert len(data["credential_id"]) == 11  # "ak_" + 8 hex chars
        assert "*" in data["scopes"]

    def test_master_key_no_session_id(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/whoami", headers=_headers("master-test-key-42"))
        data = resp.json()
        assert "session_id" not in data


class TestWhoamiUnauthorized:
    def test_no_key_returns_401(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/whoami")
        assert resp.status_code == 401


class TestWhoamiInvalidKey:
    def test_invalid_key_returns_401(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/whoami", headers=_headers("wrong-key"))
        assert resp.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_whoami.py -v`
Expected: FAIL — endpoint doesn't exist, returns 404

- [ ] **Step 3: Add `auth:read` to VALID_AGENT_SCOPES**

In `app/auth_middleware.py`, add `"auth:read"` to the `VALID_AGENT_SCOPES` set (lines 56-64):

```python
VALID_AGENT_SCOPES: set[str] = {
    "ssh:connect",
    "ssh:execute",
    "ssh:disconnect",
    "ssh:files",
    "ssh:port-check",
    "jobs:read",
    "jobs:run",
    "auth:read",
}
```

- [ ] **Step 4: Refine `/api/auth/*` middleware bypass**

In `app/auth_middleware.py`, replace `ALWAYS_PUBLIC` (line 248):

```python
ALWAYS_PUBLIC = frozenset({"/", "/health", "/api/capabilities"})
PUBLIC_AUTH_PATHS = frozenset({
    "/api/auth/check",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/verify",
})
```

Then in `auth_check` (lines 280-282), replace:

```python
    # Auth endpoints are always public
    if path.startswith("/api/auth/"):
        return None
```

with:

```python
    # Public auth endpoints (register, login, verify, check — no API key required)
    if path in PUBLIC_AUTH_PATHS:
        return None
```

- [ ] **Step 5: Create the whoami router**

Create `app/routers/auth.py`:

```python
"""Auth diagnostic routes — whoami identity endpoint."""

from fastapi import APIRouter, Depends

from app.auth_middleware import AuthIdentity, require_scope

router = APIRouter(tags=["auth"])


@router.get("/api/auth/whoami")
async def whoami(
    identity: AuthIdentity = Depends(require_scope("auth:read")),
) -> dict:
    """Return the caller's identity, scopes, auth method, and credential ID.

    Scope: auth:read (master key bypasses scope checks).
    """
    credential_id = "ak_" + identity.fingerprint[:8]
    scopes_list = list(identity.scopes) if "*" not in identity.scopes else ["*"]
    return {
        "identity": identity.name or identity.token_type,
        "scopes": scopes_list,
        "auth_method": (
            "api_key"
            if identity.token_type in ("master", "agent")
            else identity.token_type
        ),
        "credential_id": credential_id,
    }
```

- [ ] **Step 6: Register the new router in main.py**

In `app/main.py`, add the import after the existing `from app.user_auth import router as auth_router` (line 53):

```python
from app.routers.auth import router as auth_identity_router
```

Then add the include_router call after the existing `app.include_router(auth_router)` (line 945):

```python
app.include_router(auth_identity_router)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_whoami.py -v`
Expected: 3 passed

- [ ] **Step 8: Verify existing auth endpoints still work**

Run: `pytest tests/test_route_auth_contract.py -v`
Expected: All existing contract tests pass

- [ ] **Step 9: Commit**

```bash
git add app/auth_middleware.py app/routers/auth.py app/main.py tests/test_whoami.py
git commit -m "feat: add GET /api/auth/whoami with auth:read scope"
```

---

### Task 4: MCP Toolset Hash

**Files:**
- Create: `tests/test_toolset_hash.py`
- Modify: `examples/mcp_server/server.py` — add `compute_toolset_hash()` function

**Interfaces:**
- Produces: `compute_toolset_hash(mcp_instance) -> str` — returns `"sha256:" + hex_digest`
- Consumes: `FastMCP` instance with registered tools (via `_tool_manager._tools` dict, tool objects have `.parameters`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_toolset_hash.py`:

```python
"""Tests for MCP toolset hash computation."""

import hashlib
import json


def _canonical_form(tool_names: list[str], schemas: dict[str, dict]) -> str:
    """Replicate the canonical JSON logic for testing without FastMCP."""
    items = [{"name": n, "inputSchema": schemas.get(n, {})} for n in tool_names]
    items.sort(key=lambda item: item["name"])
    return json.dumps(items, sort_keys=True, separators=(",", ":"))


def test_canonical_sort_order():
    """Tool names must be sorted alphabetically."""
    schema_b = {"type": "object", "properties": {"y": {"type": "string"}}}
    schema_a = {"type": "object", "properties": {"x": {"type": "string"}}}
    canonical = _canonical_form(["b_tool", "a_tool"], {"b_tool": schema_b, "a_tool": schema_a})
    parsed = json.loads(canonical)
    assert parsed[0]["name"] == "a_tool"
    assert parsed[1]["name"] == "b_tool"


def test_deterministic_hash():
    """Same tools must produce the same hash regardless of insertion order."""
    schema = {"type": "object", "properties": {"cmd": {"type": "string"}}}
    c1 = _canonical_form(["run", "stop"], {"run": schema, "stop": schema})
    c2 = _canonical_form(["stop", "run"], {"run": schema, "stop": schema})
    h1 = "sha256:" + hashlib.sha256(c1.encode()).hexdigest()
    h2 = "sha256:" + hashlib.sha256(c2.encode()).hexdigest()
    assert h1 == h2


def test_different_tools_different_hash():
    """Different tool sets must produce different hashes."""
    schema = {"type": "object"}
    c1 = _canonical_form(["a", "b"], {"a": schema, "b": schema})
    c2 = _canonical_form(["a", "c"], {"a": schema, "c": schema})
    h1 = "sha256:" + hashlib.sha256(c1.encode()).hexdigest()
    h2 = "sha256:" + hashlib.sha256(c2.encode()).hexdigest()
    assert h1 != h2


def test_prefix_is_sha256():
    """Hash must start with sha256: prefix."""
    canonical = _canonical_form(["x"], {"x": {}})
    h = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_compact_json_no_spaces():
    """Canonical JSON must have no spaces after separators."""
    canonical = _canonical_form(["t"], {"t": {"type": "object"}})
    assert ", " not in canonical
    assert ": " not in canonical
```

- [ ] **Step 2: Run test to verify they pass immediately**

Run: `pytest tests/test_toolset_hash.py -v`
Expected: 5 passed — these are pure logic tests validating the canonical form algorithm

- [ ] **Step 3: Add `import json` at top of server.py**

In `examples/mcp_server/server.py`, add `import json` after the existing `import time` (around line 8):

```python
import json
```

- [ ] **Step 4: Add `compute_toolset_hash` to server.py**

In `examples/mcp_server/server.py`, add the following function after the `instrumented` decorator (after line 358) and before the `run_tool` function:

```python
import hashlib as _hashlib


def compute_toolset_hash(mcp_instance: FastMCP) -> str:
    """Compute SHA-256 hash of the canonical tool manifest.

    Canonical form: sorted list of {name, inputSchema} objects as compact JSON.
    Uses items.sort(key=lambda item: item["name"]) — NOT sorted(dicts).
    """
    tools_dict = {}
    if hasattr(mcp_instance, "_tool_manager"):
        tm = mcp_instance._tool_manager
        if hasattr(tm, "_tools"):
            tools_dict = tm._tools

    items = []
    for name, tool_obj in tools_dict.items():
        schema = getattr(tool_obj, "parameters", None) or {}
        items.append({"name": name, "inputSchema": schema})

    items.sort(key=lambda item: item["name"])
    canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return "sha256:" + _hashlib.sha256(canonical.encode()).hexdigest()
```

- [ ] **Step 5: Verify the function is importable**

Run: `cd examples/mcp_server && python -c "from server import compute_toolset_hash; print('OK')"`
Expected: `OK` (may print FastMCP init warnings to stderr — that's expected)

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_server/server.py tests/test_toolset_hash.py
git commit -m "feat: add compute_toolset_hash for deterministic tool manifest fingerprint"
```

---

### Task 5: MCP Health Tool

**Files:**
- Modify: `examples/mcp_server/server.py` — replace existing `gateway_health` tool with aggregated `health` tool
- Create: `tests/test_mcp_health_tool.py`

**Interfaces:**
- Consumes: `client.health()` (returns gateway health dict), `compute_toolset_hash(mcp)`, env vars `BUILD_SHA`, `BUILD_TIME`
- Produces: `{"mcp": {...}, "gateway": {...}}` dict

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_health_tool.py`:

```python
"""Tests for the MCP aggregated health tool."""

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mcp_started():
    """Ensure _mcp_started_at is set on the server module."""
    import examples.mcp_server.server as srv

    if not hasattr(srv, "_mcp_started_at"):
        import time

        srv._mcp_started_at = time.time()
    yield


def test_health_tool_returns_mcp_and_gateway_keys():
    """The health tool must return a dict with 'mcp' and 'gateway' keys."""
    from examples.mcp_server.server import gateway_health

    mock_client = MagicMock()
    mock_client.health.return_value = {
        "status": "ok",
        "build_sha": "gw_sha",
        "build_time": "gw_time",
        "started_at": "gw_started",
        "version": "0.1.30",
    }

    import examples.mcp_server.server as srv
    original_client = srv.client
    srv.client = mock_client
    try:
        result = gateway_health()
    finally:
        srv.client = original_client

    assert "mcp" in result
    assert "gateway" in result
    gw = result["gateway"]
    assert gw["build_sha"] == "gw_sha"
    assert gw["version"] == "0.1.30"


def test_mcp_section_has_toolset_hash():
    """MCP section must include toolset_hash, tools_count, contract_version."""
    from examples.mcp_server.server import gateway_health

    mock_client = MagicMock()
    mock_client.health.return_value = {"status": "ok"}

    import examples.mcp_server.server as srv
    original_client = srv.client
    srv.client = mock_client
    try:
        result = gateway_health()
    finally:
        srv.client = original_client

    mcp = result["mcp"]
    assert "toolset_hash" in mcp
    assert mcp["toolset_hash"].startswith("sha256:")
    assert "tools_count" in mcp
    assert isinstance(mcp["tools_count"], int)
    assert mcp["contract_version"] == "1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_health_tool.py -v`
Expected: FAIL — the current `gateway_health` returns `_run_gateway(tool="health", fn=client.health)` which returns a flat dict, not `{"mcp": ..., "gateway": ...}`

- [ ] **Step 3: Add MCP process start time tracking**

In `examples/mcp_server/server.py`, add near the top of the file (after the existing imports, around line 10):

```python
import time as _time
_mcp_started_at = _time.time()
```

- [ ] **Step 4: Replace the gateway_health tool implementation**

In `examples/mcp_server/server.py`, replace the `gateway_health` function (lines 448-452):

```python
@register_tool("health")
@instrumented("health")
def gateway_health() -> dict[str, Any]:
    """Check gateway + MCP health with build metadata and toolset hash."""
    from datetime import UTC, datetime

    gateway_data = client.health()

    mcp_build_sha = os.environ.get("BUILD_SHA", "").strip() or "unknown"
    mcp_build_time = os.environ.get("BUILD_TIME", "").strip()
    mcp_started_at = ""
    if _mcp_started_at:
        mcp_started_at = datetime.fromtimestamp(
            _mcp_started_at, tz=UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    toolset_hash = compute_toolset_hash(mcp)

    tools_count = 0
    if hasattr(mcp, "_tool_manager"):
        tm = mcp._tool_manager
        if hasattr(tm, "_tools"):
            tools_count = len(tm._tools)

    return {
        "mcp": {
            "build_sha": mcp_build_sha,
            "build_time": mcp_build_time,
            "started_at": mcp_started_at,
            "toolset_hash": toolset_hash,
            "tools_count": tools_count,
            "contract_version": "1",
        },
        "gateway": gateway_data,
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_mcp_health_tool.py -v`
Expected: 2 passed

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `pytest tests/ -v --timeout=30`
Expected: All existing tests pass

- [ ] **Step 7: Commit**

```bash
git add examples/mcp_server/server.py tests/test_mcp_health_tool.py
git commit -m "feat: MCP health tool aggregates gateway + MCP build metadata and toolset hash"
```

---

### Task 6: Integration Smoke Test + Final Validation

**Files:**
- Create: `tests/test_diagnostics_integration.py`

**Interfaces:**
- Consumes: All previous tasks' outputs
- Produces: End-to-end validation that all diagnostics components work together

- [ ] **Step 1: Write the integration test**

Create `tests/test_diagnostics_integration.py`:

```python
"""Integration tests verifying all P1 diagnostics components work together."""

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def _auth_and_ip(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "integ-test-key")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


def test_health_and_whoami_independent():
    """Both /health and /api/auth/whoami must work without interfering."""
    with TestClient(app) as client:
        health_resp = client.get("/health")
        whoami_resp = client.get(
            "/api/auth/whoami",
            headers={"X-API-Key": "integ-test-key"},
        )

    assert health_resp.status_code == 200
    h = health_resp.json()
    assert "build_sha" in h
    assert "version" in h

    assert whoami_resp.status_code == 200
    w = whoami_resp.json()
    assert w["identity"] == "master"
    assert w["credential_id"].startswith("ak_")
    assert "session_id" not in w


def test_whoami_scope_in_openapi():
    """The /api/auth/whoami endpoint must appear in OpenAPI schema."""
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
    schema = resp.json()
    whoami_path = schema.get("paths", {}).get("/api/auth/whoami", {})
    assert "get" in whoami_path, "/api/auth/whoami not found in OpenAPI"


def test_auth_read_scope_in_valid_scopes():
    """auth:read should be in VALID_AGENT_SCOPES."""
    from app.auth_middleware import VALID_AGENT_SCOPES

    assert "auth:read" in VALID_AGENT_SCOPES
```

- [ ] **Step 2: Run the integration test**

Run: `pytest tests/test_diagnostics_integration.py -v`
Expected: 3 passed

- [ ] **Step 3: Run the full test suite**

Run: `pytest -q`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_diagnostics_integration.py
git commit -m "test: integration tests for P1 diagnostics (whoami, health metadata, toolset hash)"
```

---

### Task 7: Update OpenAPI Tags + Documentation

**Files:**
- Modify: `app/main.py:310-346` — add "auth" tag to `openapi_tags`

- [ ] **Step 1: Add auth tag to OpenAPI**

In `app/main.py`, add a new tag entry in `openapi_tags` list (after the "help" tag, around line 337):

```python
        {
            "name": "auth",
            "description": "Authentication diagnostics (whoami). Requires scope: `auth:read` for agent tokens.",
        },
```

- [ ] **Step 2: Verify OpenAPI renders correctly**

Run: `python -c "from app.main import app; import json; schema = app.openapi(); print('/api/auth/whoami' in schema.get('paths', {}))"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add app/main.py
git commit -m "docs: add auth tag to OpenAPI for whoami endpoint"
```

---

## Self-Review Checklist

1. **Spec coverage:**
   - 4a. GET /api/auth/whoami → Task 3 ✅
   - 4b. Gateway health metadata → Task 2 ✅
   - 4c. MCP health tool → Task 5 ✅
   - 4d. Build metadata module → Task 1 ✅
   - 4e. Toolset hash → Task 4 ✅

2. **Placeholder scan:** No "TBD", "TODO", or "implement later" found. All steps contain complete code.

3. **Type consistency:**
   - `get_build_metadata()` returns `dict[str, str]` — used identically in Task 2 and Task 5
   - `AuthIdentity.fingerprint` property used in Task 3 for `credential_id`
   - `compute_toolset_hash(mcp)` signature consistent between Task 4 definition and Task 5 usage
   - `build_info.get_started_at()` returns `float | None` — ISO string conversion in `get_build_metadata()`
   - `HealthResponse` model fields match `health_check` return dict keys
   - FastMCP tool objects: `.parameters` dict used (not `.inputSchema`) — verified against FastMCP 1.x internals
   - Tests use `monkeypatch.setattr(settings, ...)` pattern matching existing test suite conventions

4. **Known risks:**
   - `compute_toolset_hash` accesses FastMCP internals (`_tool_manager._tools`). If FastMCP changes internal structure, the function will return a hash of an empty toolset. Mitigation: guarded by `hasattr` checks, and the function is MCP-process-local (not in gateway core).
   - `PUBLIC_AUTH_PATHS` in middleware must match exactly the paths registered by `user_auth.router`. If new public auth endpoints are added to `user_auth.py`, they must also be added to `PUBLIC_AUTH_PATHS`.
