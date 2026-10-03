# Tool-surface parity and legacy candidate lineage recovery — Design

## Status

Design only. No runtime code in this phase. Nothing in this document changes
what is deployed at `ccc380ef` (`master`, PR #435 merged).

Three separate delivery units are specified here:

- **PR-A** — measurable external tool-surface parity (`EXTERNAL_RESOURCE_CATALOG_*`).
- **PR-B** — legacy same-repo candidate lineage recovery (adopt, then cleanup).
- **PR-C** — live verification of `quart-core #227` through a real Gateway.

Implementation starts only after the operator reviews this document, with
particular attention to the two acceptance cases in §7.3.

## Purpose

Two independent control-plane blockers currently prevent the Gateway from being
used to update stale PR branches:

1. **Parity is not observable.** Guarded Git mutations (`git_update_branch_by_merge`,
   `git_push`, `gitea_push_verified_commit`) depend on guard tools
   (`git_fetch_ref`, `git_refresh_branch_to_head`) that exist in the server-side
   `mcp_client_write` catalog but are not callable through the external
   connector. Today the Gateway cannot tell whether a given caller can actually
   reach a guard tool, so it cannot fail closed with an accurate reason.
2. **Legacy candidate lineage recovery is unreachable.** A delivered legacy
   same-repo candidate aborts the whole lineage scan with
   `CANDIDATE_LINEAGE_SCAN_FAILED`, so nothing can be adopted or cleaned, and the
   deadlocking directory stays on disk.

The goal is not "make the tools appear". The goal is to make the discrepancy
**measurable and correctly gated**, and to make recovery **provable**. Where the
root cause of the external filtering is not yet established, this document
specifies the instrument that will establish it, and explicitly forbids
guessing a fix.

## 1. Verified current state (against `ccc380ef`)

### 1.1 Surface registration

- `git_fetch_ref` and `git_refresh_branch_to_head` are registered **only** in
  `mcp_client_write` (`examples/mcp_server/tool_modes.py:285-286`).
- Tool mode is a **process-global** read of `MCP_GATEWAY_TOOL_MODE`
  (`tool_modes.py:324-326`). There is no per-session mode.
- Registration is filtered through `should_register_tool()`
  (`examples/mcp_server/mcp_infra/tool_registry.py:73`).
- The operator confirms the external connector and the server-side manifest refer
  to the **same Gateway instance in the same mode**, so this is not a mode
  mismatch.

### 1.2 Measurements (taken against the live `mcp_client_write` catalog)

| Metric | Value |
|---|---|
| Registered tools in `mcp_client_write` | 134 |
| Registered tools in `mcp_client` | 112 |
| Total `inputSchema` bytes (all modes) | 42,135 |
| Mean `inputSchema` bytes | 314 |
| `git_fetch_ref` schema bytes / rank | 418 / 28 of 134 |
| `git_refresh_branch_to_head` schema bytes / rank | 403 / 32 of 134 |

The two guard tools are **not** schema-size outliers, which removes total-payload
size as a plausible cause. The remaining hypotheses are a catalog count cap, a
name allowlist, or a connector/authorization-side cache.

### 1.3 What the manifest does and does not know

- `tools_manifest.py` (224 lines) is pure local introspection. Its
  `operator_surface_contract` declares the external catalog as the literal
  resource `"api_tool.list_resources"` with `verified: false` and the diagnostic
  `EXTERNAL_RESOURCE_CATALOG_UNVERIFIED` (`tools_manifest.py:36-56`).
- `"api_tool.list_resources"` appears **only** as that literal. No server-side
  external export layer exists. The MCP server's own `list_tools` is its surface;
  any filtering happens in the client, the connector, or authorization.
- Consequently the Gateway **cannot** derive external visibility from its own
  manifest, and this design does not permit it to pretend otherwise.

### 1.4 The existing preflight is a different concern

`mcp_client_tools.py:1720` raises `GIT_WRITE_PREFLIGHT_UNAVAILABLE`. Inspection of
`GIT_WRITE_PREFLIGHT_UNAVAILABLE` handling shows it checks **filesystem Git
writeability** (index, objects, refs, `HEAD`) — not whether a required tool is
callable. It is not reusable as a surface-parity gate and must not be conflated
with one.

### 1.5 Verified guard-dependency analysis

Signatures were read individually, because the intuitive grouping is wrong:

- `git_push` (`mcp_client_tools.py:2723`) accepts only `project`, `remote`,
  `branch`. There is **no** `expected_head` and no CAS of any kind.
- `gitea_push_verified_commit` (`mcp_infra/adapters/remote.py:2536`) is **not** a
  local Git push — it pushes through the Gitea API using `GITEA_TOKEN`, from a
  registered clean workspace, and requires both `expected_base_sha` and
  `expected_head_sha`.
- `git_update_branch_by_merge` (`mcp_client_tools.py`) takes an **optional**
  `expected_head` that refers to the **local** branch HEAD, not a remote ref.
- `git_fetch_ref` (`mcp_client_tools.py`) is itself the tool that pins a trusted
  remote ref, subject to the filesystem preflight.

This is the input to §2.5, and it is deliberately narrow.

### 1.6 Existing reusable primitive

`compute_toolset_hash(mcp_instance)` already exists (`tool_registry.py:157`); it
is a `sha256:` over the canonical sorted list of `{name, inputSchema}`. It is
already exported (`server.py:232`) and already surfaced through the Gateway
adapter (`adapters/gateway.py:276,294`). **PR-A reuses it; it does not introduce a
second fingerprint mechanism.**

## 2. PR-A — measurable external parity

### 2.1 Contract shape

`operator_surface_contract` is split into three explicitly separate sections:

- **`server_surface`** — authoritative. `toolset_hash`, `registered_tool_count`,
  `total_schema_bytes`, and the sorted registered names. The Gateway asserts
  these without qualification; they describe this process.
- **`client_observation`** — a **client-reported attestation, never independently
  verified by the Gateway**. It records the tool names the caller says it can
  invoke, whether the caller claims the list is complete, and the server-side
  session/build binding under which that report was received. Binding proves
  provenance/freshness only; it does **not** prove the report is truthful.
  Absent report ⇒ `status: "not_supplied"`.
- **`guard_coverage`** — diagnostic coverage derived **only** from the current
  session-bound client attestation. States `reported_present`, `reported_absent`,
  or `unknown` for guard tools that a concrete workflow actually requires.
  Absent report ⇒ `status: "unknown"`, never `"ok"` or `"verified"`.

A caller may never assert its own safety. The Gateway may derive diagnostics from
the attestation, but no field named `verified` may be set true merely because a
client report is bound to a session. `external_resource_catalog_verified` remains
`false` unless a future mechanism independently observes the external catalog.

### 2.2 The observation channel

There is no protocol today by which a client tells the server which imported
tools it retained, and none can be derived server-side (`tools/list` is served in
full; what the client keeps is invisible). The channel is therefore explicit and
client-supplied:

- `tools_manifest` gains an optional `client_visible_tool_names` parameter,
  accompanied by `client_visible_tool_names_complete`.
- The response carries `client_observation` with `status: "supplied"` and the
  submitted names, plus the diff against `server_surface`.
- **Completeness is explicit and defaults to `false`.** A name absent from a
  report marked incomplete is *unknown*, not *absent*. This distinction is what
  makes §2.5 sound: the gate may only act on proven absence, and a caller who
  cannot enumerate the whole surface therefore cannot accidentally lock itself —
  or anyone else — out of Git mutations.
- No new tool is added — the observation rides an existing read-only tool, which
  keeps the catalog unchanged and avoids perturbing the very thing being measured.

**Input bounds.** `client_visible_tool_names` is untrusted request data and is
validated before any state is retained. One attestation may contain at most 256
names and at most 16 KiB of UTF-8 name bytes in aggregate; each name is stripped
of surrounding ASCII whitespace, must match the Gateway tool-name grammar and be
at most 128 bytes, then exact-string deduplicated and sorted. Unknown-but-valid
names are retained only for `unexpected_in_client` diagnostics. An invalid or
over-limit report returns `INVALID_INPUT` and performs **no** session-store write.
These bounds are intentionally above the current 134-tool catalog while preventing
one session from creating unbounded process state.

### 2.3 Binding and lifetime

A report is useful only as a diagnostic about the **same MCP transport/session**
that submitted it. `_current_auth_reuse_key()` identifies an OAuth client (or a
static-token fingerprint); it is deliberately reusable across transports and is
therefore **insufficient** as the storage key. Two parallel MCP sessions for the
same OAuth client may import different external catalogs and must never overwrite
or gate one another.

Each attestation is bound to all three of:

- `_current_auth_reuse_key()` — authenticated identity;
- a **server-created MCP lifecycle/session owner** obtained from the active
  `FastMCP` request context (the existing `_current_mcp_lifecycle_owner()` /
  SDK-created `ServerSession` lifecycle), never from the raw client-controlled
  `Mcp-Session-Id` header;
- `server_toolset_hash` — invalidates the attestation after any catalog change.

The in-process store is keyed by the server-created lifecycle owner and stores
`{auth_identity, names, complete, toolset_hash}`. The lifecycle owner is stable
for exactly one MCP transport and is removed in `_mcp_lifespan` teardown. A
second session with the same OAuth identity gets a distinct entry. On every read,
the current auth identity and `toolset_hash` are rechecked; either mismatch makes
the attestation unavailable.

If no server-created MCP session/lifecycle owner exists (startup helpers, stdio
unit seams, code outside a request), the attestation is **not stored at all** and
is returned only as an unbound one-shot diagnostic. There is no process-global or
auth-identity-only fallback. Raw `Mcp-Session-Id` is never accepted as a key.

This binding prevents cross-session reuse and stale-build reuse; it still does
**not** make the submitted tool list truthful or independently verified.

### 2.4 Diagnostics

- `EXTERNAL_RESOURCE_CATALOG_UNVERIFIED` — retained, now meaning "no bound
  observation exists for this caller".
- `EXTERNAL_RESOURCE_CATALOG_MISMATCH` — new; raised when a bound observation
  shows the registered surface and the observed surface differ. It **may enumerate
  the full diff** (`missing_from_client`, `unexpected_in_client`) for diagnostics.
- `preflight_surface_unverified` — **advisory** field, never an error, present on
  mutation results when no bound observation exists.

### 2.5 Guard diagnostics are call-context-specific, not a static tool map

`EXTERNAL_RESOURCE_CATALOG_MISMATCH` reporting the full diff does **not** imply a
full-equality requirement and, by itself, never blocks a mutation.

A missing external tool may justify `REQUIRED_PREFLIGHT_UNAVAILABLE` only when the
**concrete call path currently executing** requires that tool to establish a
precondition that is otherwise unavailable. This cannot be represented by a
static `mutation -> guard tool` table: the same mutation can be safe in one call
context and require an extra fetch/refresh in another.

The verified signatures imply:

- `gitea_push_verified_commit` receives `expected_base_sha` and
  `expected_head_sha` as explicit required inputs. Once the caller has those exact
  values, the mutation itself does **not** require `git_fetch_ref`; the SHA may
  have been established earlier by another trusted path.
- `git_update_branch_by_merge` may operate on already-local exact refs/objects.
  The absence of `git_fetch_ref` is not a failure unless this particular workflow
  first needs to obtain a remote ref that is not already available locally.
- `git_push` has no exact-head CAS precondition today. That is a separate design
  debt and is not repaired by parity attestation.
- `git_refresh_branch_to_head` is a workflow convenience and must never be treated
  as a universal prerequisite.

Therefore PR-A ships **measurement and a reusable evaluator**, not a hard-coded
mutation gate. The evaluator accepts an explicit `required_guard_tools` set from
the call site that actually knows the workflow state. It may return
`reported_absent` only when the current session-bound attestation is complete and
claims a required tool is missing. Existing mutation entry points are not wired to
this evaluator unless their implementation itself has a concrete unmet
cross-tool precondition.

If no current session-bound attestation exists, if it is incomplete, or if the
call site has no concrete required guard, the result is `unknown`/advisory and the
mutation is not blocked. This avoids both a process-wide lockout and a false
per-tool dependency.

### 2.6 Establishing the root cause — one observation, no redeploy

The initial plan assumed a controlled reduced-surface experiment would require a
temporary deployment. It does not. The observation channel from §2.2 already
yields the discriminating measurement at zero deployment cost, because the
**shape** of the observed set separates the hypotheses:

- observed set is a truncated **alphabetical prefix** ⇒ catalog count cap;
- observed set contains **only `gitea_*`** ⇒ name allowlist;
- observed set is empty or unchanged across sessions ⇒ connector/authorization
  cache.

A2 (curated external catalog) and A3 (caching) are **explicitly out of scope**
until this measurement exists. Guessing a filter and encoding it in a curated
list would bake an unverified hypothesis into the tool surface.

## 3. PR-B — legacy same-repo candidate lineage recovery

### 3.1 The deadlock, precisely

A delivered legacy candidate `candidate-quart-core-feat-supervisor-read-knowledge-capabilities-20261001`
sits in the same repository as the source root and has already been delivered:
its HEAD is reachable from the trusted `main`.

- `_find_lineage_claimant` (`candidate_clone.py:1253`) does **not** exclude a
  legacy-named candidate in the **same** repository. It skips a legacy candidate
  only when `_is_verified_foreign_repository()` is true (`:1275-1279`); same-repo
  is not foreign, so it proceeds to read metadata.
- `_read_candidate_metadata` (`:1136`) requires complete, well-formed metadata and
  raises; the scan surfaces `CANDIDATE_LINEAGE_SCAN_FAILED` and every candidate —
  including valid managed ones — becomes unscannable.
- `candidate_cleanup` (`:1627`) demands a `preserved_ref` of the form
  `archive/candidate-*`, and `_require_preserved_head` (`:879`) verifies the head
  against it.
- `_verify_candidate_clean` (`:841`) requires valid `project_id`, `source_project`,
  `branch`, and `root`, which a legacy candidate by definition does not have.

So the same missing metadata both aborts the scan and blocks the only cleanup
path. Adopt and cleanup must be separated, and **cleanup must stay destructive-
only**.

### 3.2 Correction: identity and preservation are different proofs

An earlier draft required protected-branch reachability for **both** operations.
That is wrong and would defeat the feature.

`PreservationProof` exists because **cleanup destroys**. It answers "is the HEAD
safe to lose?". Reachability from `main` is one acceptable answer to that
question, and only one of several.

**Adopt does not destroy anything.** It does not delete, does not rewrite HEAD,
does not move a branch; it only reconstructs provable lineage metadata. Requiring
`HEAD ∈ main` would make it impossible to legalize the very live, unmerged feature
candidates that recovery exists to legalize.

The correct pipeline is:

```
identity proof  →  optional adopt  →  preservation proof  →  explicit cleanup
```

not:

```
main reachability  →  adopt  →  cleanup
```

### 3.3 `IdentityProof` (required for adopt)

All conditions must hold; any failure is fail-closed with a specific code.

1. **Safe root** — `_validate_candidate_root` (`:805`): candidate root is a real
   directory under `.mcp-candidate-clones`, matching dev/ino expectations, with
   no symlink traversal or path escape.
2. **No symlinked git dir** — `_reject_symlinked_git_dir` (`:821`).
3. **Trusted identity equality** — `_legacy_trusted_remote(candidate_dir)`
   (`:1213`) equals `_resolve_trusted_remote(source_root)`: the candidate belongs
   to the **same** trusted repository as the source root. This reuses the #430/#434
   primitive rather than introducing new remote parsing.
4. **Exact HEAD** — `rev-parse HEAD` equals the operator-supplied
   `expected_head_sha`.
5. **Clean worktree** — `_status_state` (`:469`) reports no modifications,
   including no untracked files.
6. **Exact branch identity** — `rev-parse --abbrev-ref HEAD` equals the expected
   branch. A detached HEAD is rejected, not guessed.
7. **No conflicting managed metadata** — either no metadata exists, or existing
   metadata is byte-identical to what the proof would write. Conflicting
   non-empty metadata ⇒ refuse; adopt never overwrites a competing claim.
8. **No unsafe or ambiguous identity** — unresolved origin, external-path origin,
   cyclic clone chain, or over-max-hop chain ⇒ refuse.
9. **Locks and reference guards** — the existing lineage lock is held, and the
   injected `reference_guard` plus `.locks` and tombstone inspection report no
   active task, delivery, or materialization bound to this candidate.

Identity is derived **only** from git objects and remote configuration. The
directory name is never treated as evidence.

### 3.4 Preservation evidence for adopt (advisory, never main-only)

Adopt rewrites metadata and nothing else. The worktree stays byte-identical, `HEAD`
does not move, the branch does not move, and the candidate directory itself
remains on disk holding the very commit in question. **No data can be lost along
the adopt path**, so requiring external preservation evidence there would buy no
safety and would block exactly the live unmerged candidates recovery exists to
legalize.

Preservation evidence is therefore **advisory for adopt** and **mandatory for
cleanup** (§3.5). Where it can be established, adopt records it, and any one of
these trusted mechanisms suffices to establish it:

- the **exact remote feature branch** equals the candidate HEAD;
- the **exact open PR head** equals the candidate HEAD;
- the HEAD is already **reachable from a branch that the trusted Gitea adapter has
  freshly proven is protected**, with both the protection state and exact branch
  SHA pinned as in §3.5. The symbolic name `main`/`master` is never protection evidence.

Protected-branch reachability is thus one option among several, never a
prerequisite. Recorded as advisory, it also becomes the natural fast path: a
later cleanup request can reuse the already-proven reachability instead of
re-proving it.

### 3.5 `PreservationProof` (required for destructive cleanup)

Cleanup must answer "if this directory is removed, is the HEAD still
recoverable?". Exactly two constructors are permitted:

**`from_archive_ref`** — existing behaviour. `preserved_ref` matching
`archive/candidate-*`, verified by `_require_preserved_head` (`:879`).

**`from_protected_branch_reachability`** — new. It is constructed only from
trusted adapter evidence, not from an operator assertion or branch-name policy.
The adapter must query Gitea and return a `ProtectedBranchEvidence` value carrying
`branch`, `protected=true`, and the branch's **fresh exact SHA**. Core candidate
code receives this evidence (or a required verifier callback) by injection; it
must not treat `_PROTECTED_BRANCHES = {"main", "master"}` or a
`protected_branch="main"` argument as proof.

Verification steps:

1. Freshly query the trusted Gitea branch endpoint and require the branch to be
   reported protected. Missing, unprotected, or unverifiable ⇒ fail closed.
2. Pin the exact SHA returned by that same fresh query.
3. Require the candidate HEAD object to exist locally, then prove it is an
   ancestor of the pinned SHA with `git merge-base --is-ancestor`. No fetch is
   performed.
4. Immediately before `shutil.rmtree`, repeat the trusted Gitea protection+SHA
   query and the ancestor proof. The branch must still be protected and its SHA
   must still equal the pinned SHA. If it moved, protection became unknown, or
   reachability can no longer be proven, deletion is forbidden.

This second verification is part of the destructive boundary, not an optional
extra. A test must move the protected branch between the initial proof and the
filesystem delete and prove the directory remains.

If the HEAD is not delivered to a genuinely protected branch **and** has no
archive preservation, cleanup is forbidden. There is no third path.

### 3.6 Versioned tombstone compatibility

The current cleanup journal stores `preserved_ref` in a version-1 tombstone and
may be resumed from `prepared`, `registry_removed`, or `complete`. PR-B must not
strand an interrupted cleanup merely because the new code represents preservation
as a discriminated object.

The reader therefore supports both shapes:

- **legacy v1**: `preserved_ref: "archive/candidate-*"` is normalized in memory to
  `preservation = {"kind": "archive_ref", "preserved_ref": ...}`;
- **new v2**: `preservation.kind` is exactly `archive_ref` or
  `protected_branch_reachability`, with kind-specific fields validated strictly.

New writes use v2. Legacy tombstones are never rewritten just to migrate them;
they are normalized on read and may continue/resume through all three existing
phases. The identity comparison is performed against the normalized preservation
identity, not by blindly comparing the raw on-disk dictionary. Regression tests
must cover v1 tombstones in `prepared`, `registry_removed`, and `complete` across
the new code. A deployment-time assertion that no tombstones exist is **not** a
substitute for this compatibility contract.

### 3.7 Reconstructed metadata provenance

Adopt may prove the candidate's **current** repository identity, branch and exact
HEAD, but it cannot reconstruct the historical `base_ref` or `base_sha` from
which a legacy clone was originally created. The adopted metadata therefore uses
a new schema/reconstruction marker and records historical provenance as unknown;
it must not synthesize `base_ref = expected_branch`, `base_sha = HEAD`, or any
other invented history. Current facts (`source_project`, `branch`, `head`, safe
`root`) remain explicit and are the only facts used for identity/cleanup.

### 3.8 Scan behaviour for an unreadable legacy candidate

`_find_lineage_claimant` (`:1288-1295`) re-raises, so one unreadable candidate
aborts the scan for **every** lineage. That is the availability half of the
deadlock; the safety half is that simply skipping such a candidate would be wrong,
because it might genuinely claim the lineage being prepared, and skipping would
let a second clone be created for the same `source_project` + `branch`.

The resolution is to separate *cannot claim* from *cannot prove*, using git
objects rather than the directory name (per §3.3, identity never comes from the
name):

- For a legacy candidate whose metadata is unreadable or incomplete, read its
  **actual current branch** from git.
- If that branch differs from the requested branch, the candidate **cannot claim
  this lineage** ⇒ skip it. Unrelated preparations stop being poisoned, which is
  the availability fix.
- If that branch equals the requested branch, the candidate **may claim it** and
  its identity cannot be proven ⇒ **fail closed**, with a typed error naming the
  specific candidate and a `repair_action` pointing at the adopt tool from §3.3.

This is strictly narrower than today's behaviour: unrelated lineages are unblocked,
same-branch ambiguity still blocks, and no candidate is ever silently ignored while
it could be a claimant.

### 3.9 Adopt semantics

- **Non-destructive** and **idempotent**.
- Writes canonical metadata via `_write_metadata` (`:569`) **only after** the full
  identity proof passes.
- Idempotent replay: metadata already identical to the proven identity ⇒ returns
  `already_adopted`, writing nothing.
- Records a lineage-reconstruction marker, because `project_id` must equal the
  directory name (`_read_candidate_metadata`, `:1154`) and a legacy name is not
  managed-shaped (`candidate-.+` + two 12-hex suffixes). Adopt therefore writes
  the **actual** directory name as `project_id` rather than renaming the
  directory — renaming would be a move, and adopt must not move anything.
  A subsequently adopted legacy candidate scans cleanly: its suffix check no
  longer fires, its metadata now parses, and its `(source_project, branch)` simply
  does not match the scanning branch.
- Never deletes, never renames, never moves a branch, never touches `HEAD`.

### 3.10 Cleanup semantics

- Explicit, opt-in, and separate from adopt. Reuses the existing
  `_cleanup_candidate_locked` (`:1712`) path unchanged in structure: lineage lock,
  tombstone phases, registry CAS on expected head, and every existing guard.
- The only change is how preservation is established: `candidate_cleanup` accepts
  a `PreservationProof` from either constructor rather than hard-requiring an
  `archive/candidate-*` ref.
- `_unregister_candidate` (`:934`) already tolerates an absent registry entry by
  returning `already_absent`, so an adopted-but-never-registered legacy candidate
  cleans up correctly.
- A candidate with no bound `PreservationProof` is never touched.

## 4. What is explicitly out of scope

- **A2 — curated external catalog.** Not built until §2.6 measures the actual
  filter.
- **A3 — catalog caching.** Same condition.
- Per-session tool modes. The process-global mode stays; PR-A measures the
  surface rather than reshaping it.
- Any server-side inference of what an external client retained.
- Automatic cleanup. Recovery is always explicit and opt-in.
- Fetching to establish a reachability claim.
- Adding a CAS precondition to `git_push`. Its total lack of one (§1.5) is recorded
  as a finding, not fixed here — PR-A measures the surface, it does not redesign
  the tools.
- Modifying or merging `quart-core #227`.

## 5. Regression coverage

PR-B must preserve all #430/#434 behaviour and add the new cases.

| # | Case | Expected |
|---|---|---|
| 1 | Foreign legacy repository candidate | ignored, no error |
| 2 | Chained local legacy origin | trusted identity resolves, no scan failure |
| 3 | Same repo + valid managed lineage | works, unchanged |
| 4 | Same repo + legacy metadata + provable identity/state | recoverable |
| 5 | Same repo + ambiguous identity | fail-closed, no mutation |
| 6 | Symlinked `.git` or symlinked origins | rejected |
| 7 | External-path origin | rejected |
| 8 | Cyclic clone chain | rejected |
| 9 | Chain over max hop count | rejected |
| 10 | Dirty candidate | not deleted, not rewritten |
| 11 | Candidate with active task or delivery | untouched |
| 12 | Remote branch / HEAD mismatch | fail-closed |
| 13 | **Same-repo clean legacy candidate, HEAD not in `main`, exact remote feature branch or open PR preserves HEAD** | **adopt allowed; cleanup forbidden** |
| 14 | **Same-repo legacy candidate, exact HEAD reachable from a branch freshly proven protected by trusted Gitea evidence, delivery branch absent** | **adopt and explicit cleanup allowed without an archive ref; registry already-absent tolerated** |
| 14-race | **Protected branch moves or loses/obscures protection after initial proof but before delete** | **cleanup fails closed; candidate directory remains** |

Cases 13 and 14 are the pair that distinguishes a correct design from the
rejected `main-reachability-first` variant: 13 must not be blocked, and 14 must
not require an archive ref.

PR-A must cover: no-session report ⇒ one-shot/unbound and never process-global;
two simultaneous MCP sessions with the same OAuth identity and different catalogs
remain isolated; teardown removes only that session's attestation; stale
`toolset_hash` or changed auth identity invalidates the session attestation; raw
`Mcp-Session-Id` cannot select storage; input count/byte/name bounds reject before
state write; full server/client diff is reported while
`external_resource_catalog_verified` stays `false`; incomplete reports leave
omitted names `unknown`; complete reports may mark an explicitly requested
call-context guard `reported_absent`; and `gitea_push_verified_commit`,
`git_update_branch_by_merge`, and `git_push` are **not** hard-gated merely because
`git_fetch_ref` is absent from the report. No static mutation→guard map is allowed.

## 6. `TODO.md` updates

- Guarded fetch / exact-head refresh P1 — status reflects actual state, not intent.
- ChatGPT-visible parity P1 — stays **open** until §2.6 measurement plus real
  external callable evidence. Code alone does not close it.
- Same-repo candidate recovery — a **separate** TODO entry, closed only if both
  cases 13 and 14 are covered.

## 7. PR-C — live verification protocol

After PR-A and PR-B are merged and deployed to a real Gateway:

1. **exact fetch** — fetch PR #227 head `310ea37d448a6b6d2f3bec5471bc1ee67e2a649c`.
2. **guarded merge/update** — perform the merge/update through the Gateway using
   the guarded Git mutations, not raw `git`.
3. **push** — push the update.
4. **behind = 0** and **`branch_contains_base = true`**.
5. Run CI to completion on the updated branch.
6. Fetch `quart-core` `main` and prove **exactly one** new commit whose parent is
   the pre-merge `main`, whose tree equals the updated PR head, and whose author
   is the **verified commit** signature on the PR head.
7. Record the returned `toolset_hash`.

Baseline for step 4, before the update: `main` was
`76d7245fb7fd220782777fab508df0354e157cfd`, PR head `310ea37d…`, state
`ahead_by=1`, `behind_by=1`, `branch_contains_base=false`. `quart-core` itself is
never modified or merged by this work.

PR #433 (`chore/reconcile-pr400-stale-20261003`, worktree
`<workspace-root>/asg-reconcile`) and its stale `#400` branch are
updated to current `master` **after** this verification succeeds.

## 8. Risks and open questions

- **The root cause of the external filtering is still unknown.** PR-A ships the
  instrument, not the fix. If §2.6 shows a count cap, A2 becomes a real proposal;
  until then it is not designed.
- **A 134-name report is unwieldy.** The operator may be unable to enumerate the
  whole surface. Because `client_visible_tool_names_complete` defaults to `false`,
  an incomplete observation yields `unknown` coverage for unmentioned dependencies
  and can neither block a mutation nor prove one safe. The trade-off is deliberate:
  the instrument is easy to submit honestly and hard to submit dangerously.
- **A parity report is a point-in-time observation.** A connector that reloads
  tools mid-session could invalidate it. This is accepted for now and is why the
  report binds to `toolset_hash` and identity rather than being cached long-term.
- **Adopted legacy candidates keep legacy-shaped names.** This is a deliberate
  trade for non-destructiveness; a future rename operation is out of scope.
- **Proxmox host access is unavailable** without credentials, so KVM-guest
  accounting behind the CPU pressure that destabilises Selenium CI cannot be
  completed from here.

## 9. Sources

- Code inspection at `ccc380ef`: `tool_modes.py`, `mcp_infra/tool_registry.py`,
  `server.py`, `mcp_client_tools.py`, `tool_results.py`,
  `mcp_infra/adapters/gateway.py`, `control_plane_git.py`, `candidate_clone.py`.
- Live `mcp_client_write` catalog measurement (section 1.2).
- Operator confirmation: same Gateway instance and mode; the delivered legacy
  candidate's HEAD is reachable from the trusted `main`.
- Merged prior work: PR #430 `490aebb0`, PR #434 `6646681d`, PR #435 `ccc380ef`.