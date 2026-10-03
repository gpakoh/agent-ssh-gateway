# Tool-Surface Parity Implementation Plan

> **For agentic workers:** implement task-by-task. This plan is intentionally diagnostic-first: client-supplied visibility is an attestation, not independently verified truth.

**Goal:** make the external MCP tool-surface discrepancy measurable without creating cross-session state leaks or false mutation dependencies.

**Architecture:** `surface_parity.py` owns bounded client attestations and diagnostic diff/coverage. Attestations are bound to three values: authenticated identity, the **server-created MCP lifecycle owner**, and the current `compute_toolset_hash(mcp)`. The store is lifecycle-scoped and cleared at MCP transport teardown. `tools_manifest` renders authoritative server state plus explicitly client-reported state. No current Git mutation is hard-gated merely because an adjacent tool is missing; guard requirements are evaluated only when a concrete call path explicitly declares a tool is actually required.

**Base:** `master` at `ccc380ef` unless updated before implementation.

## Non-negotiable constraints

- Never trust raw `Mcp-Session-Id`. Use `_current_mcp_lifecycle_owner()` / the SDK-created session lifecycle.
- `_current_auth_reuse_key()` is **not** a session identity. Two parallel MCP transports for one OAuth client must keep independent attestations.
- If no server-created MCP lifecycle exists, do not retain the attestation in process state.
- Client visibility is never independently verified. `external_resource_catalog_verified` remains `false`.
- `client_visible_tool_names` bounds: max 256 names; max 16 KiB aggregate UTF-8 name bytes; max 128 bytes/name; exact-string dedupe and sort; reject invalid/over-limit input before state mutation.
- Unknown-but-valid names are allowed only so `unexpected_in_client` is measurable.
- No static `GUARD_DEPENDENCIES = {mutation: ...}` table. The same mutation may or may not require a fetch/refresh depending on call context.
- `gitea_push_verified_commit` already receives exact `expected_base_sha` and `expected_head_sha`; it is not intrinsically dependent on `git_fetch_ref`.
- `git_update_branch_by_merge` may operate on already-local exact state; absence of `git_fetch_ref` is not automatically a failure.
- `git_push` lacks a CAS guard today; that debt is out of scope.

---

## Task 1 — bounded session-scoped attestation store

**Files**
- Create `examples/mcp_server/surface_parity.py`
- Create `tests/test_surface_parity.py`

### Interfaces

Implement:

```python
MAX_CLIENT_VISIBLE_TOOL_NAMES = 256
MAX_CLIENT_VISIBLE_TOOL_NAME_BYTES = 128
MAX_CLIENT_VISIBLE_TOOL_NAMES_BYTES = 16 * 1024

@dataclass(frozen=True)
class ClientSurfaceAttestation:
    auth_identity: str
    toolset_hash: str
    names: tuple[str, ...]
    complete: bool


def normalize_client_visible_tool_names(names: list[str]) -> tuple[str, ...]: ...
def record_session_attestation(
    lifecycle_owner: object,
    auth_identity: str,
    toolset_hash: str,
    names: list[str],
    complete: bool,
) -> ClientSurfaceAttestation: ...
def load_session_attestation(
    lifecycle_owner: object | None,
    auth_identity: str | None,
    toolset_hash: str | None,
) -> ClientSurfaceAttestation | None: ...
def clear_session_attestation(lifecycle_owner: object) -> None: ...
def clear_all_attestations_for_tests() -> None: ...
```

The dictionary key is the server-created lifecycle-owner object itself. The value also stores `auth_identity` and `toolset_hash`; both are revalidated on every load.

### Required tests

- same lifecycle + same auth + same hash round-trips;
- stale hash returns `None`;
- changed auth returns `None`;
- two different lifecycle owners with the **same auth identity** retain different catalogs;
- clearing one lifecycle does not affect the other;
- `lifecycle_owner=None` cannot be stored;
- 257 names rejected;
- >16 KiB aggregate rejected;
- >128-byte name rejected;
- invalid tool-name grammar rejected;
- duplicates normalized once and sorted;
- unknown-but-valid names retained.

Run:

```bash
pytest tests/test_surface_parity.py -q
```

---

## Task 2 — lifecycle teardown owns cleanup

**Files**
- Modify `examples/mcp_server/server.py`
- Extend `tests/test_surface_parity.py`

### Change

Import `clear_session_attestation`. In `_mcp_lifespan`, the existing `lifecycle_owner = object()` is the attestation session key. In `finally`, clear the attestation **before** any cancellable network cleanup:

```python
clear_session_attestation(lifecycle_owner)
```

Do not derive a key from a request header or from `id(lifecycle_owner)`.

### Required tests

- attestation exists during lifecycle;
- teardown removes it even when Gateway SID cleanup is cancelled/fails;
- parallel lifecycles for one OAuth identity do not cross-delete.

---

## Task 3 — manifest contract: authoritative server state vs client attestation

**Files**
- Modify `examples/mcp_server/tools_manifest.py`
- Modify `tests/test_tools_manifest.py`

### Contract

`operator_surface_contract` contains:

```text
server_surface             authoritative server-local MCP catalog
client_observation         client-reported attestation; never independently verified
guard_coverage             diagnostic result for explicitly requested call-context guards
external_resource_catalog_verified = false
```

`client_observation` should expose `status`, `complete`, normalized names, and binding status, but not a raw auth identity or internal lifecycle object.

The response includes full diff:

```text
missing_from_client
unexpected_in_client
```

When no current session-bound attestation exists: `status=not_supplied` (or `supplied_unbound` for a one-shot report outside an MCP lifecycle), and `guard_coverage.status=unknown`.

### Required tests

- `server_surface` reflects exact local tool names/hash;
- client report never flips `external_resource_catalog_verified` to true;
- complete report produces full diff;
- incomplete report does not treat omitted names as proven absent;
- unbound report never populates retained session state.

---

## Task 4 — `tools_manifest` accepts and records bounded reports

**Files**
- Modify `examples/mcp_server/server.py`
- Extend `tests/test_surface_parity.py`

### Signature

Add:

```python
client_visible_tool_names: list[str] | None = None
client_visible_tool_names_complete: bool = False
required_guard_tool_names: list[str] | None = None
```

`required_guard_tool_names` is diagnostic call-context input only. Bound it with the same tool-name grammar and a small max count (e.g. 32). It does not create a global dependency map.

### Flow

1. compute live `toolset_hash` with existing `compute_toolset_hash(mcp)`;
2. get `_current_auth_reuse_key()`;
3. get `_current_mcp_lifecycle_owner()`;
4. normalize/validate client names before writes;
5. if lifecycle owner + auth identity + hash all exist, store under that lifecycle;
6. otherwise render the submitted report as unbound one-shot diagnostic only;
7. pass current session-bound attestation into manifest rendering.

### Required tests

- same OAuth identity, two lifecycle owners, different reports => each manifest sees only its own report;
- no MCP lifecycle => no retained report;
- invalid/oversize report leaves previous valid session attestation unchanged;
- stale hash is ignored.

---

## Task 5 — call-context guard evaluator, no mutation wiring yet

**Files**
- Modify `examples/mcp_server/surface_parity.py`
- Extend tests

Implement:

```python
def evaluate_required_guards(
    attestation: ClientSurfaceAttestation | None,
    required_guard_tools: tuple[str, ...],
) -> dict[str, Any]: ...
```

Rules:

- empty requirement => `status=no_dependency`;
- no attestation => `unknown`;
- incomplete attestation => omitted names remain `unknown`;
- complete attestation => required name present => `reported_present`; missing => `reported_absent`;
- wording must say **reported**, not verified/proven.

Do **not** call this from `gitea_push_verified_commit`, `git_update_branch_by_merge`, or `git_push` in PR-A. Their current signatures do not establish a universal cross-tool dependency. A future workflow that actually needs a remote fetch can pass `required_guard_tools=("git_fetch_ref",)` at the exact orchestration call site.

Add a source-level regression test asserting no static `GUARD_DEPENDENCIES` table and no parity gate invocation inside those three mutations.

---

## Task 6 — diagnostics/error-code registration

**Files**
- Modify `examples/mcp_server/tool_results.py`
- Extend tests

Register `EXTERNAL_RESOURCE_CATALOG_MISMATCH` if the manifest/tool envelope uses it. Do **not** raise `REQUIRED_PREFLIGHT_UNAVAILABLE` from existing Git mutation entry points in this PR.

Diagnostic semantics:

- `EXTERNAL_RESOURCE_CATALOG_UNVERIFIED`: no current session-bound attestation;
- `EXTERNAL_RESOURCE_CATALOG_MISMATCH`: client-reported names differ from server names;
- mismatch is informational unless a concrete workflow separately declares a required guard.

---

## Task 7 — acceptance and regression suite

Required acceptance coverage:

1. two concurrent MCP sessions with same OAuth client, different catalogs, zero cross-session overwrite;
2. teardown removes exactly that session's attestation;
3. raw/fake `Mcp-Session-Id` input cannot select another session's attestation;
4. no-session helper invocation does not create process-global attestation;
5. report limits and normalization are enforced before state write;
6. client report is always labelled client-reported/unverified;
7. full server/client diff is emitted;
8. incomplete report omission stays unknown;
9. `gitea_push_verified_commit` is **not** blocked merely because `git_fetch_ref` is absent from the client report;
10. `git_update_branch_by_merge` is **not** blocked when exact required state is already local;
11. no static mutation→guard dependency map exists.

Run:

```bash
pytest tests/test_surface_parity.py tests/test_tools_manifest.py -q
ruff check examples/mcp_server tests
mypy examples/mcp_server/surface_parity.py examples/mcp_server/tools_manifest.py
pytest -q
```

Update `TODO.md` to say measurement shipped but external parity remains open until a real connector report identifies the filtering mechanism.

---

## Post-merge measurement

On a real external connector, submit the complete visible tool list once per MCP session and record:

- `server_toolset_hash`;
- `missing_from_client`;
- `unexpected_in_client`;
- whether the observed shape is consistent with count cap, allowlist, or cache.

Do not implement a curated catalog or caching workaround until that evidence exists.
