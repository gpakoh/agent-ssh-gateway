# C2.3 Agent Usability — SDK + MCP for Preview/Verify/Safe Write

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose preview/verify and safe-write receipts to agents through Python SDK helpers (Stage A) and MCP tools (Stage B), then document everything (Stage C).

**Architecture:** Three independent stages — SDK static methods on `SSHGatewayClient`, MCP tools wrapping `app.workspace.preview.*`, and docs in `api_help.py`. All stages call the REST endpoints delivered by C2.2 (PR #11, commit `c6335c5` — specifically `87e6142` and `4e71a04`).

**Tech Stack:** Python `requests` (SDK), FastMCP (MCP), `app.workspace.preview`, `app.workspace.edit`, `examples/mcp_server/`

## Global Constraints

- No new REST endpoints — all called endpoints exist at `POST /api/workspace/projects/{id}/files/preview/write|edit|patch` and `/files/verify` (C2.2, commit `87e6142`)
- No rollback, no snapshot, no deploy/restart/docker in scope
- `safe=False` by default — C1 clients see identical response
- SDK: methods are `@staticmethod` on `SSHGatewayClient`, no auth except `api_key` header override
- MCP: tools live in `examples/mcp_server/server.py` with `@register_tool` decorator
- Response: pass-through of REST response schema — no custom wrapping
- No content leak: preview/verify responses are metadata-only (hashes, diffs, sizes)

---

## Stage A: SDK Helpers — Agent 2

Implement `SSHGatewayClient` static methods (and `quick` class mirror) for preview/verify and safe-write. Test scaffold already exists at `tests/test_sdk_workspace.py` (untracked, 12 tests). Reference REST endpoints: C2.2 PR #11, commit `87e6142`.

### SDK Contracts

| Method | Signature | Returns |
|---|---|---|
| `SSHGatewayClient.workspace_preview_write` | `(project_id, path, content, max_bytes=1_000_000, base_url, api_key)` | REST preview/write dict |
| `SSHGatewayClient.workspace_preview_edit` | `(project_id, path, old_string, new_string, max_bytes=1_000_000, base_url, api_key)` | REST preview/edit dict |
| `SSHGatewayClient.workspace_preview_patch` | `(project_id, path, patch, max_bytes=1_000_000, base_url, api_key)` | REST preview/patch dict |
| `SSHGatewayClient.workspace_verify` | `(project_id, path, expected_hash, base_url, api_key)` | REST verify dict |
| `SSHGatewayClient.workspace_write` | `(project_id, path, content, max_bytes=1_000_000, safe=False, base_url, api_key)` | REST write dict (+receipt if safe) |
| `SSHGatewayClient.workspace_edit` | `(project_id, path, old_string, new_string, max_bytes=1_000_000, safe=False, base_url, api_key)` | REST edit dict (+receipt if safe) |
| `SSHGatewayClient.workspace_patch` | `(project_id, path, patch, max_bytes=1_000_000, safe=False, base_url, api_key)` | REST patch dict (+receipt if safe) |

Same methods must also exist on `SSHGatewayClient.quick.*` for one-shot convenience.

### Task A1: Add SDK static methods to SSHGatewayClient

**Files:**
- Modify: `sdk/ssh_gateway.py` (after `disconnect` method, before `BackgroundJob`)

- [ ] **Step 1: Add `workspace_preview_write` static method**

```python
    @staticmethod
    def workspace_preview_write(
        project_id: str,
        path: str,
        content: str,
        max_bytes: int = 1_000_000,
        base_url: str = "https://gateway.example.com",
        api_key: str = "",
    ) -> dict:
        """Preview a file write without writing to disk.

        Returns diff, hashes, and size changes. No disk mutation.
        """
        client = SSHGatewayClient(base_url)
        if api_key:
            client.session.headers["X-API-Key"] = api_key
        r = client.session.post(
            f"{base_url}/api/workspace/projects/{project_id}/files/preview/write",
            json={"path": path, "content": content, "max_bytes": max_bytes},
        )
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 2: Add `workspace_preview_edit` static method**

```python
    @staticmethod
    def workspace_preview_edit(
        project_id: str,
        path: str,
        old_string: str,
        new_string: str,
        max_bytes: int = 1_000_000,
        base_url: str = "https://gateway.example.com",
        api_key: str = "",
    ) -> dict:
        """Preview a file edit without writing to disk.

        Returns diff, hashes, and size changes. No disk mutation.
        """
        client = SSHGatewayClient(base_url)
        if api_key:
            client.session.headers["X-API-Key"] = api_key
        r = client.session.post(
            f"{base_url}/api/workspace/projects/{project_id}/files/preview/edit",
            json={
                "path": path,
                "old_string": old_string,
                "new_string": new_string,
                "max_bytes": max_bytes,
            },
        )
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 3: Add `workspace_preview_patch` static method**

```python
    @staticmethod
    def workspace_preview_patch(
        project_id: str,
        path: str,
        patch: str,
        max_bytes: int = 1_000_000,
        base_url: str = "https://gateway.example.com",
        api_key: str = "",
    ) -> dict:
        """Preview a patch application without writing to disk.

        Returns diff, hashes, and size changes. No disk mutation.
        """
        client = SSHGatewayClient(base_url)
        if api_key:
            client.session.headers["X-API-Key"] = api_key
        r = client.session.post(
            f"{base_url}/api/workspace/projects/{project_id}/files/preview/patch",
            json={"path": path, "patch": patch, "max_bytes": max_bytes},
        )
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 4: Add `workspace_verify` static method**

```python
    @staticmethod
    def workspace_verify(
        project_id: str,
        path: str,
        expected_hash: str,
        base_url: str = "https://gateway.example.com",
        api_key: str = "",
    ) -> dict:
        """Verify a file's current hash matches expected hash.

        Returns matches, current_hash, file_exists.
        """
        client = SSHGatewayClient(base_url)
        if api_key:
            client.session.headers["X-API-Key"] = api_key
        r = client.session.post(
            f"{base_url}/api/workspace/projects/{project_id}/files/verify",
            json={"path": path, "expected_hash": expected_hash},
        )
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 5: Add `workspace_write` static method with `safe` param**

```python
    @staticmethod
    def workspace_write(
        project_id: str,
        path: str,
        content: str,
        max_bytes: int = 1_000_000,
        safe: bool = False,
        base_url: str = "https://gateway.example.com",
        api_key: str = "",
    ) -> dict:
        """Write (create or overwrite) a file inside a project.

        Args:
            safe: if True, include nested receipt in response.
        """
        client = SSHGatewayClient(base_url)
        if api_key:
            client.session.headers["X-API-Key"] = api_key
        r = client.session.post(
            f"{base_url}/api/workspace/projects/{project_id}/files/write",
            json={"path": path, "content": content, "max_bytes": max_bytes, "safe": safe},
        )
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 6: Add `workspace_edit` static method with `safe` param**

```python
    @staticmethod
    def workspace_edit(
        project_id: str,
        path: str,
        old_string: str,
        new_string: str,
        max_bytes: int = 1_000_000,
        safe: bool = False,
        base_url: str = "https://gateway.example.com",
        api_key: str = "",
    ) -> dict:
        """Edit a file by replacing first occurrence of old_string.

        Args:
            safe: if True, include nested receipt in response.
        """
        client = SSHGatewayClient(base_url)
        if api_key:
            client.session.headers["X-API-Key"] = api_key
        r = client.session.post(
            f"{base_url}/api/workspace/projects/{project_id}/files/edit",
            json={
                "path": path,
                "old_string": old_string,
                "new_string": new_string,
                "max_bytes": max_bytes,
                "safe": safe,
            },
        )
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 7: Add `workspace_patch` static method with `safe` param**

```python
    @staticmethod
    def workspace_patch(
        project_id: str,
        path: str,
        patch: str,
        max_bytes: int = 1_000_000,
        safe: bool = False,
        base_url: str = "https://gateway.example.com",
        api_key: str = "",
    ) -> dict:
        """Apply a unified diff patch to a file.

        Args:
            safe: if True, include nested receipt in response.
        """
        client = SSHGatewayClient(base_url)
        if api_key:
            client.session.headers["X-API-Key"] = api_key
        r = client.session.post(
            f"{base_url}/api/workspace/projects/{project_id}/files/patch",
            json={"path": path, "patch": patch, "max_bytes": max_bytes, "safe": safe},
        )
        r.raise_for_status()
        return r.json()
```

### Task A2: Add quick.* convenience wrappers

**Files:**
- Modify: `sdk/ssh_gateway.py` (add `quick` nested class or extend existing one)

The `quick` class is a convenience short-hand that bundles connect → action → disconnect. Add the same 7 methods as `@staticmethod` on `class quick`. Each creates a one-shot `SSHGatewayClient`, sets `api_key` header, calls the corresponding static method, and returns the result.

- [ ] **Step 1: Wire existing `quick` methods or add new ones**

Add to the `class quick` block (after existing `quick.run` and `quick.read`):

```python
    @staticmethod
    def workspace_preview_write(
        project_id: str,
        path: str,
        content: str,
        max_bytes: int = 1_000_000,
        base_url: str = "https://gateway.example.com",
        api_key: str = "",
    ) -> dict:
        return SSHGatewayClient.workspace_preview_write(
            project_id, path, content, max_bytes, base_url, api_key
        )

    # ... repeat for workspace_preview_edit, workspace_preview_patch,
    #     workspace_verify, workspace_write, workspace_edit, workspace_patch
```

### Task A3: Run SDK tests

**Files:**
- Test: `tests/test_sdk_workspace.py` (already exists as untracked TDD scaffold)

- [ ] **Step 1: Add the test file to tracking and run it**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m pytest tests/test_sdk_workspace.py -v
```

Expected: 12 passed

- [ ] **Step 2: Commit Stage A**

```bash
git add sdk/ssh_gateway.py tests/test_sdk_workspace.py
git commit -m "feat(SDK): add workspace preview/verify/safe-write helpers

Stage A of C2.3 — 7 static methods on SSHGatewayClient + quick.*
convenience wrappers. All call REST endpoints from C2.2 (PR #11,
commit 87e6142). 12 tests in test_sdk_workspace.py."
```

---

## Stage B: MCP Preview/Verify + Safe Param — Agent 3

Add `safe` param to existing `workspace_file_write/edit/apply_patch` MCP tools, add 4 new preview/verify MCP tools, register scopes and modes.

### Task B1: Add `safe` param to existing MCP write/edit/patch tools

**Files:**
- Modify: `examples/mcp_server/server.py:2596-2715`

- [ ] **Step 1: Add `safe: bool = False` to `gateway_workspace_file_write`**

Signature + pass-through to `project_file_write(..., safe=safe)`.

- [ ] **Step 2: Add `safe: bool = False` to `gateway_workspace_file_edit`**

Signature + pass-through to `project_file_edit(..., safe=safe)`.

- [ ] **Step 3: Add `safe: bool = False` to `gateway_workspace_apply_patch`**

Signature + pass-through to `project_apply_patch(..., safe=safe)`.

### Task B2: Add MCP preview/verify tools

**Files:**
- Modify: `examples/mcp_server/server.py` (after existing workspace tools)

Add 4 new tools: `workspace_preview_write`, `workspace_preview_edit`, `workspace_preview_patch`, `workspace_verify`. Each calls the corresponding `app.workspace.preview.*` function via `_get_workspace_registry()`.

Preview patch must strip `patch` key from response (same as `workspace_apply_patch`).

### Task B3: Register scopes for new MCP tools

**Files:**
- Modify: `examples/mcp_server/tool_scopes.py`

Add to `TOOL_SCOPES`:
```python
    "workspace_preview_write": ["mcp:read", "mcp:project"],
    "workspace_preview_edit": ["mcp:read", "mcp:project"],
    "workspace_preview_patch": ["mcp:read", "mcp:project"],
    "workspace_verify": ["mcp:read", "mcp:project"],
```

### Task B4: Register modes for new MCP tools

**Files:**
- Modify: `examples/mcp_server/tool_modes.py`

Add to `"full"` mode set: `workspace_file_write`, `workspace_file_edit`, `workspace_preview_write`, `workspace_preview_edit`, `workspace_preview_patch`, `workspace_verify`.

### Task B5: Tests for MCP workspace tools

**Files:**
- Create: `tests/test_mcp_workspace_tools.py`

Tests:
- ToolRegistration: verify all 6 tools are registered in `full` mode
- ToolScopes: verify each has correct scope
- MCPSafeWrite: verify `safe=True` is passed through
- MCPPreviewTools: verify each calls the correct `app.workspace.preview.*` function
- PreviewPatchStripsPatch: verify `patch` key is removed from response

### Task B6: Run full test matrix and commit Stage B

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m pytest tests/test_sdk_workspace.py tests/test_mcp_workspace_tools.py -v
ruff check examples/mcp_server/ app/
mypy app/
```

Expected: All pass. Then commit.

---

## Stage C: Docs/Help — Agent 4

Only after Stage A and B code is merged into master.

**Files:** `app/api_help.py`

Add/update `api_help.py` entries for:
- SDK method documentation (preview/verify/safe-write patterns)
- MCP tool listing for workspace preview/verify
- Cross-reference: "See SDK section for Python client examples"
- Rollback/snapshot disclaimers (same pattern as existing C2.2 help)

---

## Delivery Table

| Stage | What | Who | Depends On | Status |
|---|---|---|---|---|
| A | SDK static methods on `SSHGatewayClient` + `quick.*` | Agent 2 | C2.2 REST endpoints (PR #11, `c6335c5`) | Not implemented |
| B | MCP tools: preview/verify + safe param on write/edit/patch | Agent 3 | C2.2 REST endpoints, `app.workspace.preview` | Not implemented |
| C | Docs in `api_help.py` | Agent 4 | Stage A + B code merged | Not started |

## Open Risks

1. **Stage A SDK file not yet touched** — `sdk/ssh_gateway.py` on master has no preview/verify methods. The test file `tests/test_sdk_workspace.py` is a TDD scaffold written ahead of implementation.
2. **`safe=True` forces `before_content` read** — bounded to 10MB by `_exact_read`, acceptable.
3. **Preview uses `validate_read`, write uses `validate_write`** — agent tokens need both `project:read` and `project:write` for full workflow. Documented.
4. **No mode registration for `standard`/`chatgpt`** — write tools only in `full` mode. Intentional.
