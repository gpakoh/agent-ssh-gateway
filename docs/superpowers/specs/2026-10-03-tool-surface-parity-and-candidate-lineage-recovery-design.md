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
- **`client_observation`** — **unverified until bound**. The set of tool names the
  caller reports it can actually invoke, plus the identity it was bound to and
  the `toolset_hash` it was bound against. Absent report ⇒
  `status: "not_supplied"`.
- **`guard_coverage`** — derived **only** from a bound `client_observation`.
  States, per guarded mutation, whether its specific guard dependency is covered
  by the observed surface. Absent report ⇒ `status: "unknown"`, never `"ok"`.

A caller may never assert its own safety. `guard_coverage` is computed by the
Gateway from the observation; a caller cannot submit a coverage claim.

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

### 2.3 Binding (why a report cannot be spoofed)

An unbound report is worthless as a safety input: any other authenticated caller
could submit a fabricated list to unlock mutations for itself or for a peer.
Each observation is therefore bound to:

- `_current_auth_reuse_key()` — the authenticated identity of the submitting
  caller, so one caller's observation never gates another caller's mutations;
- `server_toolset_hash` — so a report is invalidated by any redeploy or catalog
  change and cannot outlive the build it described.

A report is discarded and treated as `not_supplied` when its binding does not
match the current identity **or** the current `toolset_hash`. Either mismatch is
independently disqualifying: a stale hash catches a redeploy or catalog change, a
mismatched identity catches a caller submitting on someone else's behalf.

**Storage and lifetime.** Bound observations live in a small in-process store
keyed by `_current_auth_reuse_key()`, holding `{names, complete, toolset_hash}`.
The most recent submission for an identity replaces its predecessor. Nothing is
persisted: a stored observation would outlive the connector state it describes,
which is precisely the staleness the `toolset_hash` binding exists to catch.
Process-local storage also matches the existing process-global tool mode; a
per-connection store would be the better long-term design but is out of scope for
PR-A.

The gate in §2.5 resolves its input by reading this store under
`_current_auth_reuse_key()` and re-validating the hash on **every** mutation, so
the check cannot be satisfied once and then relied upon after a redeploy.

### 2.4 Diagnostics

- `EXTERNAL_RESOURCE_CATALOG_UNVERIFIED` — retained, now meaning "no bound
  observation exists for this caller".
- `EXTERNAL_RESOURCE_CATALOG_MISMATCH` — new; raised when a bound observation
  shows the registered surface and the observed surface differ. It **may enumerate
  the full diff** (`missing_from_client`, `unexpected_in_client`) for diagnostics.
- `preflight_surface_unverified` — **advisory** field, never an error, present on
  mutation results when no bound observation exists.

### 2.5 The gate: guard dependencies only

`EXTERNAL_RESOURCE_CATALOG_MISMATCH` reporting the full diff does **not** imply a
full-equality requirement. A caller missing 90 unrelated tools must not be
blocked from merging if every guard dependency it needs is present.

`REQUIRED_PREFLIGHT_UNAVAILABLE` is raised by a guarded mutation **only** when
all three of the following hold:

1. a bound `client_observation` exists for the current identity **and**
   `toolset_hash`; **and**
2. that observation is declared **complete**; **and**
3. that complete observation **lacks the specific guard tool** this mutation
   depends on.

Dropping condition 2 would make the gate fire on unproven absence: a partial
report that simply never mentions `git_fetch_ref` is not evidence that the caller
cannot reach it. Because completeness defaults to `false`, the gate is inert
until a caller positively asserts it has enumerated the whole surface — which is
the correct bias, since a false `REQUIRED_PREFLIGHT_UNAVAILABLE` blocks legitimate
work while a missing one merely loses a diagnostic.

Verified map — a guarded dependency exists only where the mutation's
compare-and-swap precondition is established by another tool. This was
determined by reading each signature, not by assuming a workflow shape:

| Guarded mutation | CAS precondition | Tool establishing it |
|---|---|---|
| `gitea_push_verified_commit` | `expected_base_sha` **and** `expected_head_sha`, both **required** | `git_fetch_ref` — pins a trusted remote ref whose SHA becomes `expected_base_sha` |
| `git_update_branch_by_merge` | `expected_head`, **optional**, and **local** | none — the local HEAD is readable via `info(project)` |
| `git_push` | **none** | none |

Consequences, all of them narrowing:

- Only `gitea_push_verified_commit` has a genuine cross-tool guard dependency.
- `git_refresh_branch_to_head` gates **nothing**. It is a workflow convenience for
  placing a branch on a fetched commit; no mutation's precondition requires it, so
  treating its absence as a safety failure would be a fabricated dependency.
- `git_update_branch_by_merge` is **not** gated on `git_fetch_ref`. Its
  precondition is local state, so a caller without `git_fetch_ref` can still merge
  safely against a known local HEAD.
- `git_push` carries no exact-head guard whatsoever (`mcp_client_tools.py:2723`
  takes only `project`, `remote`, `branch`). This is a real observation about the
  tool surface, recorded in §8; fixing it is **out of scope** for PR-A, which
  measures rather than redesigns.

The gate therefore applies to a single mutation today. That is the correct
consequence of the dependency analysis, and it is precisely why PR-A cannot be
used as a broad Git lockout.

When **no** bound observation exists, the mutation **proceeds** and returns
`preflight_surface_unverified` as an advisory. This is the explicit prohibition
on converting "an adjacent tool happens to be missing" into a global Git
lockout: without evidence of absence there is no failure.

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
- the HEAD is already **reachable from a freshly resolved protected branch**
  (resolved and pinned exactly as in §3.5 — never inferred from the symbolic name).

Protected-branch reachability is thus one option among several, never a
prerequisite. Recorded as advisory, it also becomes the natural fast path: a
later cleanup request can reuse the already-proven reachability instead of
re-proving it.

### 3.5 `PreservationProof` (required for destructive cleanup)

Cleanup must answer "if this directory is removed, is the HEAD still
recoverable?". Exactly two constructors are permitted:

**`from_archive_ref`** — existing behaviour. `preserved_ref` matching
`archive/candidate-*`, verified by `_require_preserved_head` (`:879`).

**`from_protected_branch_reachability`** — new. Steps:

1. Freshly resolve the protected branch through `_probe_remote_ref` (`:403`).
   It must return `FOUND`; `UNKNOWN` or `ABSENT` ⇒ fail closed.
2. Pin the **exact resolved SHA**. No conclusion is ever drawn from the symbolic
   name `main`/`master`.
3. Prove `candidate HEAD` is an ancestor of that exact SHA via
   `git merge-base --is-ancestor`.
4. If the required object is not present locally, **fail closed**. No fetch is
   performed; a reachability claim must rest on evidence already in hand.

If the HEAD is not delivered to a protected branch **and** has no archive
preservation, cleanup is forbidden. There is no third path.

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
| 14 | **Same-repo legacy candidate, HEAD reachable from freshly resolved protected branch** | **adopt and explicit cleanup allowed without an archive ref** |

Cases 13 and 14 are the pair that distinguishes a correct design from the
rejected `main-reachability-first` variant: 13 must not be blocked, and 14 must
not require an archive ref.

PR-A must cover: unbound report ⇒ `not_supplied`; report bound to a different
identity ⇒ discarded; report bound to a stale `toolset_hash` ⇒ discarded; full
diff reported while `guard_coverage` remains satisfied ⇒ mutation **not** blocked;
complete bound report missing `git_fetch_ref` ⇒ `gitea_push_verified_commit` fails
with `REQUIRED_PREFLIGHT_UNAVAILABLE` **before** any push attempt; **incomplete**
bound report omitting `git_fetch_ref` ⇒ `gitea_push_verified_commit` **not** blocked
(unknown ≠ absent); `git_update_branch_by_merge` and `git_push` **never** gated on
`git_fetch_ref` absence, per the verified dependency map; no report at all ⇒
mutation proceeds with `preflight_surface_unverified`.

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
`/media/ssd120/tmp/opencode/asg-reconcile`) and its stale `#400` branch are
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