# Phase C2 — Safe IDE Workflow

> **Date:** 2026-07-15
> **Status:** Draft / Target Architecture — ADR decisions closed, implementation in progress
> **Depends on:** Phase C1 (write/edit/patch tools), Phase B (git tools), Phase A (project registry)
> **Audience:** Agent 2 (Receipt Storage & In-Memory Cap), Agent 3 (Rollback/Snapshot/Cleanup), Agent 4 (Audit & CI Hardening)

---

## 1. Executive Summary

Phase C1 gave the agent raw write/edit/patch — but no safety net. The agent writes a file and trusts it worked. The agent edits a file and has no receipt of what changed. The agent patches a file and cannot undo.

Phase C2 wraps every mutation in a **safe IDE workflow**:

```
preview → apply → receipt → verify → (commit | rollback)
```

Every write/edit/patch operation returns a **ChangeReceipt** — a metadata-only fingerprint (hashes, diff summary, verification status) with a `snapshot_id` referencing rollback content held in the `SnapshotStore`. The agent can inspect a preview before applying, verify the file was written correctly after apply, and roll back to the previous state if verification fails.

New C2 REST/MCP wrappers default to `safe=True`; existing C1 library tools default to `safe=False` for backward compatibility. C1 callers that pass `safe=False` get the exact C1 response unchanged. The `workspace:snapshot` scope controls snapshot operations, and `project:rollback` controls rollback operations separately from `project:write`.

---

## 2. Core Concepts

### 2.1 Change Receipt

A metadata-only dataclass/dict returned by every C2 write/edit/patch operation when `safe=True`. Contains before/after fingerprint but **no file content** — rollback bytes live in `SnapshotStore`, referenced by `snapshot_id`.

```python
@dataclass(frozen=True)
class ChangeReceipt:
    receipt_id: str                # UUID
    project_id: str
    path: str                      # project-relative
    relative_path: str             # alias
    operation: Literal["write", "edit", "patch", "rollback"]

    # Before
    file_exists_before: bool
    size_before: int | None
    sha256_before: str | None

    # After
    size_after: int
    sha256_after: str
    encoding: str                  # always "utf-8"

    # Snapshot reference (rollback content lives in SnapshotStore)
    snapshot_id: str | None        # None if file was new (delete on rollback)

    # Verify (read-back comparison)
    verified: bool                 # read_back.sha256 == sha256_after
    read_back_duration_ms: int

    # Audit
    timestamp: str                 # ISO-8601
    identity_name: str | None
    identity_token_type: str | None
```

### 2.2 SnapshotStore

Rollback content is stored in the `SnapshotStore` — an in-memory registry keyed by `snapshot_id`. Each snapshot holds:

- `snapshot_id: str` — UUID, also referenced from `ChangeReceipt.snapshot_id`
- `content: str` — the original file content (or `None` for new files)
- `file_exists_before: bool`
- `sha256_before: str | None`
- `sha256_after: str` — expected current hash (used for staleness check)
- `receipt_id: str` — back-reference to the receipt that created this snapshot

The `ChangeReceipt` itself is **metadata-only** — no file content. Rollback bytes live exclusively in the `SnapshotStore`.

A global memory byte cap limits total content in the `SnapshotStore` across all snapshots; when exceeded, the oldest snapshot is evicted (LRU).

On-disk snapshot files (`.ssh-gateway-snapshots/{path}.{timestamp}.snap`) are created only during rollback as a pre-rollback backup, not during the initial safe apply.

### 2.3 Rollback Policy

Rollback restores content from the `SnapshotStore` using the `snapshot_id` in a `ChangeReceipt`. If the receipt's `snapshot_id` is `None` (file was new), rollback deletes the file.

Rollback **must** check staleness before writing: read the current file's sha256 and compare against the snapshot's `sha256_after`. If they differ, reject with `STALE_SNAPSHOT` — the file was modified after the receipt was created.

Rollback does NOT traverse symlinks, does NOT create directories, does NOT write to hidden paths.

### 2.4 Audit Trail

Every C2 operation (preview, apply, verify, rollback, snapshot) appends an entry to the gateway-level audit trail. The audit trail is an append-only JSON lines file at `{data_dir}/.ssh-gateway-audit/operations.jsonl` where `data_dir` is the configured gateway data directory (e.g. `/var/lib/ssh-gateway` or `./data`).

Each audit line **excludes** file content, patch body, old_string, new_string, and rollback_content — only metadata is recorded:

```json
{
  "timestamp": "2026-07-15T12:00:00Z",
  "operation": "write",
  "project_id": "my-project",
  "path": "src/main.py",
  "sha256_before": "abc...",
  "sha256_after": "def...",
  "verified": true,
  "identity": {"name": "agent", "type": "agent", "fp_prefix": "a1b2c3d4e5f6"},
  "session_id": "sess_xxx"
}
```

The audit trail is persistent (survives gateway restart).

---

## 3. Implementation Status

The table below tracks what exists today. Everything outside the **Designed** column is a forward-looking target for C2.

| Component | Designed | Implemented (proto) | Wired REST | Wired MCP | Tested CI | Release-ready |
|-----------|----------|---------------------|------------|-----------|-----------|---------------|
| `ChangeReceipt` dataclass | ✅ | partial¹ | — | — | — | — |
| `ChangePreview` dataclass | ✅ | — | — | — | — | — |
| `project_file_preview` | ✅ | — | — | — | — | — |
| Safe write/edit/patch (`safe=`) | ✅ | partial² | — | — | — | — |
| `project_file_verify` | ✅ | — | — | — | — | — |
| `project_file_rollback` | ✅ | partial³ | — | — | — | — |
| Snapshot (`save/list/load`) | ✅ | ✅⁴ | — | — | — | — |
| WorkspaceAuditLogger | ✅ | ✅⁵ | — | — | — | — |
| REST endpoints (all C2) | ✅ | — | — | — | — | — |
| MCP tools (all C2) | ✅ | — | — | — | — | — |
| SDK `ChangeReceipt` helpers | — | — | — | — | — | — |
| Auth scopes (`project:rollback`, `workspace:snapshot`) | ✅ | — | — | — | — | — |
| Rollback staleness check (`STALE_SNAPSHOT`) | ✅ | ✅³ | — | — | — | — |
| SnapshotStore (in-memory, keyed by snapshot_id) | ✅ | ✅⁴ | — | — | — | — |
| Memory cap + LRU eviction | ✅ | — | — | — | — | — |
| Audit JSONL to disk | ✅ | ✅⁵ | — | — | — | — |
| Production hardening (DOS bounds, snapshot purge, rotation) | — | — | — | — | — | — |

**Notes:**

¹ `ChangeReceipt` prototype exists but includes `rollback_content` (must be removed — receipt must be metadata-only with `snapshot_id` instead). `receipt_id` field missing.

² `safe=` parameter exists but returns a simplified dict — not yet a full metadata-only `ChangeReceipt`. Safe default for C1 library tools must be `False`; C2 wrappers may default `True`.

³ Rollback exists in `app/workspace/snapshot.py` but has a confirmed **BLOCKER**: new-file rollback can delete a file that was created by a different agent after the original snapshot. The staleness check must distinguish between "file does not exist because it was new" and "file does not exist because it was deleted by someone else". Fix: always check `file_exists_before` and the current file's sha256 against `sha256_after`.

⁴ SnapshotStore prototype stores content in-memory but is not yet keyed by immutable `snapshot_id`. The receipt-to-snapshot link is missing.

⁵ `WorkspaceAuditLogger` is implemented but uses unbounded in-memory buffering when `log_path=None`; memory limit enforcement is a to-do.

**Key gaps (must close before C2 can ship):**

1. **Rollback staleness BLOCKER** — `rollback()` must verify current file hash against `sha256_after` before every write, including new-file rollback (distinguish "never existed" from "was deleted").
2. **REST/MCP wiring** — zero C2 endpoints are reachable over HTTP or MCP.
3. **Auth scopes** — `project:rollback` and `workspace:snapshot` are not registered in policy.
4. **Receipt correctness** — `ChangeReceipt` must be metadata-only (no `rollback_content`), include `receipt_id` and `snapshot_id`. Rollback content lives in `SnapshotStore`.
5. **CI separation** — workspace tools tests are not portable. Host-level smoke tests (real `<workspace-root>` filesystem, mTLS, auth boundary) **cannot** run in GitHub CI — they require Gitea/self-hosted runner or manual execution. GitHub CI covers only portable tests (temp registry, no external SSH).

---

## 4. API Contracts

### 4.1 Preview

Preview shows what a write/edit/patch would do **without making any changes**.

#### `project_file_preview`

```python
def project_file_preview(
    project_id: str,
    relative_path: str,
    operation: Literal["write", "edit", "patch"],
    content: str | None = None,     # for write
    old_string: str | None = None,   # for edit
    new_string: str | None = None,   # for edit
    patch: str | None = None,        # for patch
    registry: WorkspaceRegistry | None = None,
) -> ChangePreview:
```

**Returns:**

```python
@dataclass(frozen=True)
class ChangePreview:
    project_id: str
    path: str
    operation: str

    # Current state
    file_exists: bool
    size_before: int | None
    sha256_before: str | None

    # Predicted after
    size_after: int | None
    sha256_after: str | None

    # Diff
    diff: str                      # unified diff or empty

    # Warnings
    warnings: list[str]            # binary, size threshold, hidden path
    can_apply: bool                # false if policy would block
    policy_blocked_by: str | None  # "symlink", "traversal", "hidden", "scope", None
```

**Important:** Preview computes the before state (reads the file) and predicts the after state (applies the operation in memory), but does **not** write anything. The `diff` field uses `_make_diff` from C1.

#### `project_file_preview` Implementation Rules

1. Validate path against policy first — if traversal/symlink, return `can_apply=False` with `policy_blocked_by` set, do NOT raise.
2. Read current file content; if file does not exist, `file_exists=False`, `size_before=None`, `sha256_before=None`.
3. Apply operation in memory:
   - **write**: use new content directly
   - **edit**: replace old_string with new_string in current content
   - **patch**: apply unified diff to current content
4. Compute `sha256_after` from predicted content.
5. Return diff between current and predicted content.
6. Check for warnings: binary content, size > `max_bytes`, path is hidden/secret.
7. Never write, never create directories, never snapshot.

### 4.2 Safe Apply (Write / Edit / Patch)

The existing C1 `project_file_write`, `project_file_edit`, `project_file_apply_patch` functions get a **new optional parameter** `safe: bool = False`.

- When `safe=True`: follow the safe workflow and return a full `ChangeReceipt` (receipt is metadata-only; rollback content lives in `SnapshotStore`).
- When `safe=False` (default for C1 library tools): return the existing C1 dict unchanged — **no breaking change** for callers that depend on the exact C1 response shape.

Future C2 REST/MCP wrappers may default `safe=True`; the library-level default stays `False` for backward compatibility.

Safe workflow:

1. **Before snapshot:** if file exists, read current content + compute `sha256_before` + store content in `SnapshotStore` (generates `snapshot_id`).
2. **Apply:** run the existing C1 operation (preserves all current validation).
3. **Read-back verify:** Re-read the file from disk. If `read_back.sha256 == sha256_after`, `verified=True`.
4. **Receipt:** Build and return a metadata-only `ChangeReceipt` (no `rollback_content` field).

#### Response Shape for `safe=True`

```json
{
  "project_id": "my-project",
  "path": "src/main.py",

  "receipt_id": "rec_abc123",
  "operation": "write",

  "file_exists_before": true,
  "size_before": 1024,
  "sha256_before": "abc123...",

  "size_after": 2048,
  "sha256_after": "def456...",
  "encoding": "utf-8",

  "snapshot_id": "snap_def456",

  "verified": true,
  "read_back_duration_ms": 2,

  "timestamp": "2026-07-15T12:00:00Z",
  "identity_name": "agent",
  "identity_token_type": "agent",

  "diff": "@@ -1 +1 @@\n-print('hello world')\n+print('goodbye world')\n"
}
```

The C1-only fields (`size`, `path`, `project_id`, `encoding`, `diff`, `replaced`, `applied`, `backup_hash`) remain present for backward compatibility. New C2 fields are additive.

### 4.3 ChangeReceipt Helpers

```python
# Reconstructed from the receipt dict
class ChangeReceipt:
    ...

    def can_rollback(self) -> bool:
        """True if snapshot_id is not None (file existed before)."""

    def is_verified(self) -> bool:
        """True if read-back hash matches after hash."""

    def to_audit_line(self) -> dict:
        """Summary for audit log (excludes content, diff)."""
```

### 4.4 Verify

```python
def project_file_verify(
    project_id: str,
    relative_path: str,
    receipt: dict,             # the ChangeReceipt dict
    registry: WorkspaceRegistry | None = None,
) -> dict:
```

Returns `{"path": ..., "verified": bool, "current_sha256": str, "expected_sha256": str, "receipt_timestamp": ...}`.

Re-reads the file from disk and compares its sha256 against the receipt's `sha256_after`. Also checks that the file exists (if receipt said it should) or does not exist (if receipt said it was a new file).

### 4.5 Rollback

```python
def project_file_rollback(
    project_id: str,
    relative_path: str,
    receipt: dict,             # the ChangeReceipt dict from a previous apply
    registry: WorkspaceRegistry | None = None,
) -> dict:
```

**Rollback Policy:**

1. Validate path against policy (reject traversal, symlink, hidden).
2. Look up `SnapshotStore` entry by `receipt.snapshot_id`. If `snapshot_id` is `None` (file was new), rollback = delete the file. If `snapshot_id` is not found (evicted or expired), reject with `SNAPSHOT_EVICTED`.
3. Check **staleness**: read the current file's sha256. If `current_sha256 != snapshot.sha256_after`, reject with `STALE_SNAPSHOT` — the file was modified after the receipt was created. **This check is mandatory before every write.**
4. **Pre-rollback backup:** save the current (post-apply) content to an on-disk snapshot file (`.ssh-gateway-snapshots/{path}.{timestamp}.snap`) before overwriting.
5. Write `snapshot.content` back using `_atomic_write`. If content is `None` (new file deletion), delete the file instead.
6. Read-back verify: confirm sha256 matches `snapshot.sha256_before`.
7. Return a rollback receipt:

```json
{
  "path": "src/main.py",
  "rolled_back": true,
  "receipt_id": "rec_rollback_xxx",
  "sha256_before_rollback": "def456...",
  "sha256_after_rollback": "abc123...",
  "verified": true,
  "snapshot_path": ".ssh-gateway-snapshots/src/main.py.2026-07-15T12-05-00Z.snap"
}
```

### 4.6 Snapshot (Direct)

```python
def project_file_snapshot(
    project_id: str,
    relative_path: str,
    registry: WorkspaceRegistry | None = None,
) -> dict:
```

Creates a snapshot of the current file state without any mutation. Returns `{"path": ..., "snapshot_path": ..., "sha256": ..., "timestamp": ...}`.

Used for manual checkpointing by the agent.

### 4.7 Snapshot List

```python
def project_list_snapshots(
    project_id: str,
    relative_path: str | None = None,
    registry: WorkspaceRegistry | None = None,
) -> list[dict]:
```

Returns all snapshots for a project (or a specific file). Each entry has `path`, `snapshot_path`, `sha256`, `timestamp`, `size`.

### 4.8 Audit Query

```python
def project_get_audit(
    project_id: str,
    path: str | None = None,
    limit: int = 50,
    registry: WorkspaceRegistry | None = None,
) -> list[dict]:
```

Returns audit trail entries, most recent first. Supports filtering by path.

---

## 5. REST Surface

All new endpoints under `/api/workspace/projects/{project_id}/`:

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/files/write` | `project:write` | Existing C1 + `safe` param |
| POST | `/files/edit` | `project:write` | Existing C1 + `safe` param |
| POST | `/files/patch` | `project:write` | Existing C1 + `safe` param |
| POST | `/files/preview` | `project:write` | Preview without mutation |
| POST | `/files/verify` | `project:read` | Verify a receipt |
| POST | `/files/rollback` | `project:rollback` | Rollback by receipt |
| POST | `/files/snapshot` | `workspace:snapshot` | Manual snapshot |
| GET  | `/files/snapshots` | `project:read` | List snapshots |
| GET  | `/files/audit` | `project:read` | Query audit trail |

The `safe` parameter on write/edit/patch is a JSON body field (`"safe": true`).

### 5.1 Preview Endpoint

```
POST /api/workspace/projects/{project_id}/files/preview
Body: {
    "path": "src/main.py",
    "operation": "edit",
    "old_string": "hello",
    "new_string": "world"
}
Response: 200 ChangePreview
```

### 5.2 Verify Endpoint

```
POST /api/workspace/projects/{project_id}/files/verify
Body: {
    "path": "src/main.py",
    "receipt": { ... }       # the full ChangeReceipt dict
}
Response: 200 { "verified": bool, "current_sha256": ..., "expected_sha256": ... }
```

### 5.3 Rollback Endpoint

```
POST /api/workspace/projects/{project_id}/files/rollback
Body: {
    "path": "src/main.py",
    "receipt": { ... }       # the full ChangeReceipt dict
}
Response: 200 rollback receipt
```

---

## 6. MCP Surface

Three new MCP tools:

| Tool | Scope | Description |
|------|-------|-------------|
| `workspace_file_preview` | `project:write` | Preview without writing |
| `workspace_file_verify` | `project:read` | Verify a receipt |
| `workspace_file_rollback` | `project:rollback` | Rollback by receipt |

Existing `workspace_file_write`, `workspace_file_edit`, `workspace_apply_patch` gain an optional `safe` parameter. C2 MCP wrappers may default `safe=True`; pass `safe=False` to get the exact C1 response shape.

---

## 7. SDK Surface

```python
# Preview
gateway.workspace_file_preview(project_id, path, operation="write", content="...")

# Safe apply (returns receipt)
receipt = gateway.workspace_file_write(project_id, path, content, safe=True)

# Verify
verify_result = gateway.workspace_file_verify(project_id, path, receipt)

# Rollback
rollback_result = gateway.workspace_file_rollback(project_id, path, receipt)

# Audit
audit = gateway.workspace_get_audit(project_id, path="src/main.py", limit=10)
```

The SDK helper `ChangeReceipt` class provides `.can_rollback()`, `.is_verified()`, `.to_audit_line()`.

---

## 8. Receipt Storage

Receipts are **memory-only** in C2. The caller (agent, SDK) must keep the receipt dict if they want to verify or rollback later. Receipts are evicted on gateway restart. No per-receipt memory cap is needed because receipts are metadata-only (no file content). The `SnapshotStore` has its own memory cap (see §9.8).

PostgreSQL-backed receipt persistence is deferred to Phase D.

The audit trail is always persisted (JSON lines file, append-only). The audit trail survives gateway restarts.

---

## 9. Security & Threat Model

### 9.1 Receipt Trust

The receipt is generated server-side and signed only by the content hash. The caller trusts the receipt because it came from the server. If the caller loses the receipt, rollback is impossible — there is no "admin undo" API.

**Risk:** A compromised agent token could forge a receipt with a fake `sha256_before` to trick rollback into restoring arbitrary content.
**Mitigation:** Rollback always re-reads the current file and checks `current_sha256 == receipt.sha256_after` before restoring. This ensures the file state matches what the receipt describes. If the receipt's `sha256_after` does not match the current file, rollback is rejected. This makes receipt forgery useless for rollback attacks — the attacker would need to also modify the current file to match the forged receipt's `sha256_after`, at which point they already have write access.

### 9.2 Rollback Staleness

If the file was modified (by another agent, another session, or manually) after the receipt was issued, rollback is rejected. This prevents accidental overwrites of newer changes.

### 9.3 Rollback Path Safety

Rollback re-validates the path through `WorkspacePolicy.validate_write` before writing. Symlink escape, traversal, and hidden path checks apply.

### 9.4 Snapshot Security

Snapshots are stored inside the project directory (under `.ssh-gateway-snapshots/`) and inherit the project's secret path filtering. The `.ssh-gateway-snapshots/` directory is added to the secret path patterns list so it is not visible in tree/file listings.

The audit trail is stored at gateway level (`{data_dir}/.ssh-gateway-audit/`) — outside any project directory — so it cannot be tampered with from within a project.

### 9.5 Audit Integrity

The audit trail is append-only JSON lines. No deletion or modification of past entries. If disk fills up, audit logging degrades gracefully (log warning, skip write).

### 9.6 Content Leak Prevention

- The `ChangeReceipt` is **metadata-only** — no file content, no `rollback_content`, no patch body.
- The receipt's `diff` field is computed server-side from the before/after content and may contain line-level diffs.
- The audit trail excludes content, diff, patch body, old_string, and new_string — only hashes and metadata.
- Rollback content lives exclusively in the `SnapshotStore` (in-memory, never persisted to disk except during pre-rollback backup).

### 9.7 Race Conditions

Between "before snapshot" and "apply", another agent could modify the file. The read-back verify step catches this: if `verified=False`, the caller knows the file state does not match expectations. They can retry or investigate.

Rollback detects this via the staleness check (current sha256 vs receipt's sha256_after).

### 9.8 Denial of Service

- SnapshotStore in-memory storage: bounded by a global memory byte cap (configurable via `SNAPSHOT_MEMORY_CAP_BYTES`, default 64 MiB). When the cap is exceeded, the oldest snapshot is evicted (LRU).
- On-disk snapshot file storage: max 1 pre-rollback backup per file path, max 10 per project, max `max_bytes` per file.
- Audit log is bounded by `limit` on query; log rotation is deferred to Phase D.

---

## 10. Implementation Tasks (for Session 2+)

### 10.1 Core: `app/workspace/receipts.py` (new)

- `ChangeReceipt` dataclass — **metadata-only**: `receipt_id`, `snapshot_id`, hashes, timestamps, identity. No `rollback_content`.
- `ChangePreview` dataclass
- `compute_sha256(content: str) -> str`
- `read_before_state(path: Path) -> dict` — returns `{exists, size, sha256}`
- `verify_read_back(path: Path, expected_sha256: str) -> dict` — re-reads file, returns `{verified, sha256, duration_ms}`
- `build_receipt(...)` — constructs the receipt dict (snapshot must already exist)
- In-memory receipt store (metadata only, no memory cap needed per-receipt)

**Tests:** `tests/test_workspace_receipts.py` — hash computation, before/after state, read-back verify on real files, receipt shape (no content fields).

### 10.2 Core: `app/workspace/snapshot.py` (new)

- `SnapshotStore` class — in-memory registry keyed by `snapshot_id`
  - Store fields: `snapshot_id`, `content`, `file_exists_before`, `sha256_before`, `sha256_after`, `receipt_id`
  - Global memory cap (`SNAPSHOT_MEMORY_CAP_BYTES`, default 64 MiB), LRU eviction
  - `store_snapshot(...) -> snapshot_id` — stores content, returns ID
  - `get_snapshot(snapshot_id) -> SnapshotEntry | None`
  - `evict_oldest()` — called when memory cap exceeded
- `save_on_disk_backup(project_root, relative_path, content) -> Path` — pre-rollback backup to `.ssh-gateway-snapshots/`
- `list_snapshots(project_root, relative_path) -> list`

**Tests:** `tests/test_workspace_snapshot.py` — store/retrieve snapshot, memory cap eviction, on-disk backup, new-file handling (content=None).

### 10.3 Core: `app/workspace/audit.py` (new)

- `WorkspaceAuditLogger` (rename from `AuditLogger` to avoid conflict with `app.security.AuditLogger`)
- `write_audit_entry(data_dir: Path, entry: dict) -> None`
- `query_audit(data_dir: Path, path=None, limit=50) -> list[dict]`
- `_ensure_audit_dir(data_dir: Path) -> Path`
- Thread-safe append via `open(path, "a")`
- Entry builder excludes all content fields: `content`, `diff`, `patch`, `old_string`, `new_string`, `rollback_content`
- When `log_path=None`: disable in-memory buffering entirely (no audit, no crash)

**Tests:** `tests/test_workspace_audit.py` — write entries, query by path, verify ordering, verify no content fields leaked, verify `log_path=None` does not buffer.

### 10.4 Core: Modify `app/workspace/edit.py`

- Add `safe: bool = False` parameter to `project_file_write`, `project_file_edit`, `project_apply_patch`.
- When `safe=True`: snapshot before (store in `SnapshotStore`), compute receipt, read-back verify, return metadata-only receipt.
- When `safe=False` (default for C1 library tools): return existing C1 dict (no change).

### 10.5 REST: Modify `app/routers/workspace.py`

- Accept `safe` in request body for write/edit/patch endpoints.
- Add preview/verify/rollback endpoints.
- Add snapshot/snapshots/audit endpoints.

### 10.6 Auth: Add `workspace:snapshot` and `project:rollback` scopes

- Add `"workspace:snapshot"` and `"project:rollback"` to `VALID_AGENT_SCOPES` and `ALL_SCOPES`.
- `project_file_rollback` requires `project:rollback` scope.
- `project_file_snapshot` requires `workspace:snapshot` scope.

### 10.7 Tests: `tests/test_workspace_receipts.py`, `tests/test_workspace_snapshot.py`, `tests/test_workspace_audit.py`

- Unit tests for each module.
- Integration tests for safe apply → verify → rollback cycle.
- Security tests for rollback staleness, path safety, snapshot bounds.

---

## 11. ADR Decisions

All open decisions from the initial draft are closed as follows:

| # | Question | ADR |
|---|----------|-----|
| 1 | Receipt content | **Metadata-only.** No `rollback_content`, no patch body, no old/new strings. `ChangeReceipt` has `receipt_id`, `snapshot_id`, hashes, timestamps, identity. Rollback bytes live exclusively in `SnapshotStore`. |
| 2 | Audit format | **JSONL.** File content, patch body, old_string, new_string, diff, and rollback content are explicitly excluded — metadata only (hashes, identity, operation, path, verified status). |
| 3 | Audit location | **Gateway-level data dir**, not inside project. Path: `{data_dir}/.ssh-gateway-audit/operations.jsonl`. This prevents tampering from within a project. |
| 4 | SnapshotStore memory cap | **Global byte cap on SnapshotStore** (configurable `SNAPSHOT_MEMORY_CAP_BYTES`, default 64 MiB). LRU eviction when exceeded. On-disk pre-rollback backups: 1 per file, 10 per project. |
| 5 | Rollback scope | **Separate scopes.** `project:rollback` for rollback operations, `workspace:snapshot` for snapshot operations. Not just `project:write`. |
| 6 | Safe mode default | **C1 library tools default `safe=False`** (backward compatible). **C2 REST/MCP wrappers may default `safe=True`.** |
| 7 | Retention | **Memory receipts**: evicted on restart only (metadata, no cap needed). **SnapshotStore**: evicted on restart or memory cap. **Audit JSONL**: persistent (survives restart). Log rotation deferred to Phase D. |

---

## 12. Compatibility with C1

- **No breaking changes:** C1 API unchanged when `safe=False`.
- **No removed fields:** C1 response fields remain in C2-safe response.
- **No new dependencies:** C2 uses only stdlib (`hashlib`, `json`, `time`).
- **Existing tests continue to pass:** C1 tests call without `safe`, get C1 response.
- **Existing MCP tools unchanged:** `workspace_file_write` without `safe` returns current dict.
