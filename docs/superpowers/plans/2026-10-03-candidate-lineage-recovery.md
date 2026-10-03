# Legacy Candidate Lineage Recovery Implementation Plan

> **For agentic workers:** implement in order. This plan preserves every existing cleanup guard and adds recovery without inventing historical lineage.

**Goal:** recover a clean legacy same-repo candidate by proving its current identity, adopting canonical reconstructed metadata non-destructively, and permitting explicit cleanup only after an independent preservation proof.

**Architecture:** keep `candidate_clone.py` as the single lineage implementation. Separate `IdentityProof` from `PreservationProof`. Protected-branch cleanup requires fresh trusted Gitea protection evidence plus an exact SHA and is revalidated immediately before deletion. Cleanup tombstones are versioned and backward-compatible.

**Base:** `master` at `ccc380ef` unless updated before implementation.

## Non-negotiable constraints

- Pipeline is exactly: `IdentityProof -> adopt -> PreservationProof -> cleanup`.
- Adopt must **not** require `HEAD` to be reachable from `main/master`.
- Adopt never deletes, renames, moves a branch, touches `HEAD`, or registers an invented historical base.
- Directory name is not identity evidence.
- `_write_metadata` takes `_metadata_path(candidate_root)`, never the candidate root.
- Workspace tests use `_workspace_root(config_dir)` and seed a valid `projects.yaml`/registry root.
- Cleanup keeps lineage lock, reference guard, active-task/delivery checks, registry CAS, exact-head checks, delivery-branch absence checks, candidate root dev/ino checks, and existing archive-ref behavior.
- `_PROTECTED_BRANCHES = {"main", "master"}` is naming policy only. It is **not** proof a branch is protected.
- Operator-supplied `protected_branch="main"` is not protection evidence.
- No fetch is performed to make reachability provable.
- Immediately before `shutil.rmtree`, preservation is freshly revalidated.
- Legacy cleanup tombstones must resume under the new code; “verify none exist before deploy” is not an acceptable substitute.

---

## Task 1 — proof value objects and trusted protected-branch evidence

**Files**
- Modify `examples/mcp_server/candidate_clone.py`
- Add/modify trusted Gitea adapter boundary in the existing remote/Gitea adapter layer
- Create `tests/test_candidate_lineage_recovery.py`

### Core values

Add:

```python
@dataclass(frozen=True)
class IdentityProof:
    source_project: str
    branch: str
    head_sha: str
    candidate_dir_name: str

@dataclass(frozen=True)
class ProtectedBranchEvidence:
    branch: str
    protected: bool
    sha: str

@dataclass(frozen=True)
class PreservationProof:
    kind: Literal["archive_ref", "protected_branch_reachability"]
    preserved_ref: str | None = None
    protected_branch: str | None = None
    protected_sha: str | None = None
```

`ProtectedBranchEvidence` is created by a trusted adapter after a fresh Gitea branch lookup. The adapter must require `protected is True` and a full exact commit SHA. Core cleanup receives either the evidence or a required verifier callback that can freshen it. No boolean supplied by the caller is trusted.

### Required tests

- branch named `main` but Gitea says `protected=False` => no reachability proof;
- branch missing/unverifiable => fail closed;
- protected branch with exact SHA => evidence accepted;
- archive ref constructor remains unchanged and rejects non-`archive/candidate-*` refs.

---

## Task 2 — versioned cleanup tombstones, backward compatible

**Files**
- Modify cleanup tombstone read/write/identity helpers in `candidate_clone.py`
- Extend tests

### Contract

Current v1 tombstones contain:

```json
{"version": 1, "preserved_ref": "archive/candidate-...", "phase": "prepared|registry_removed|complete", ...}
```

New writes use v2:

```json
{
  "version": 2,
  "preservation": {
    "kind": "archive_ref|protected_branch_reachability",
    "preserved_ref": null,
    "protected_branch": null,
    "protected_sha": null
  },
  "phase": "prepared|registry_removed|complete"
}
```

Implement a normalization helper:

```python
def _normalize_cleanup_tombstone(data: dict[str, Any]) -> NormalizedCleanupTombstone: ...
```

Rules:

- v1 `preserved_ref` is interpreted as `archive_ref` proof;
- v2 validates discriminated fields strictly;
- unknown version/kind => fail closed;
- identity comparison uses normalized fields, not raw dict shape;
- v1 is not rewritten just for migration;
- resume works for `prepared`, `registry_removed`, and `complete`.

### Required regression tests

Create legacy v1 tombstones in each phase and prove the new code resumes/idempotently completes rather than returning permanent `WORKSPACE_CONTENDED` solely because the schema changed.

---

## Task 3 — prove legacy candidate identity without historical invention

**Files**
- Modify `candidate_clone.py`
- Extend tests

Implement `prove_candidate_identity(...)` using existing primitives:

1. `_validate_candidate_root`;
2. `_reject_symlinked_git_dir`;
3. trusted same-repository equality via `_legacy_trusted_remote(candidate)` and `_resolve_trusted_remote(source_root)`;
4. exact operator-supplied `expected_head_sha` equals current `rev-parse HEAD`;
5. clean worktree including untracked files;
6. exact current branch equals `expected_branch`; detached HEAD rejected;
7. unresolved/external/cyclic/over-hop legacy origin rejected;
8. no conflicting managed metadata;
9. lineage lock held and required `reference_guard` plus active `.ai-bridge` evidence checks clear.

### Metadata provenance

Adopted metadata records **current known facts** only. Use a schema/reconstruction marker, for example:

```json
{
  "version": 2,
  "project_id": "<actual directory name>",
  "source_project": "...",
  "branch": "...",
  "head": "<exact current HEAD>",
  "root": ".mcp-candidate-clones/<dir>",
  "lineage_reconstructed": true,
  "historical_provenance": {
    "kind": "unknown",
    "base_ref": null,
    "base_sha": null
  }
}
```

Do **not** synthesize historical `base_ref` or `base_sha` from current branch/HEAD.

---

## Task 4 — non-destructive idempotent adopt

**Files**
- Modify `candidate_clone.py`
- Extend tests

Implement:

```python
adopt_candidate_lineage(
    project_id,
    expected_head_sha,
    expected_branch,
    expected_source_project,
    *,
    config_dir,
    journal_root,
    reference_guard,
)
```

Behavior:

- takes no archive/protected preservation parameter;
- acquires the lineage lock;
- resolves workspace using `_workspace_root(config_dir)`;
- proves identity;
- invokes required reference guard;
- writes metadata with `_write_metadata(_metadata_path(candidate_root), data)`;
- second identical call returns `already_adopted=True` with no write;
- never adds the legacy candidate to the registry merely to make cleanup possible.

### Required tests

- wrong expected head rejected;
- dirty candidate rejected;
- active task/delivery rejected;
- symlinked `.git` rejected;
- external path/origin, cycle, max-hop rejected;
- exact current HEAD/branch unchanged after adopt;
- directory remains;
- idempotent replay;
- metadata contains explicit unknown historical provenance, not invented base fields.

---

## Task 5 — branch-scoped lineage scan recovery

**Files**
- Modify `_find_lineage_claimant`
- Extend tests

For unreadable legacy metadata:

- inspect actual current branch from git;
- if current branch is safely readable and differs from requested branch, skip because it cannot claim this lineage;
- if branch matches, is detached, or cannot be safely proven, fail closed with typed `CANDIDATE_LINEAGE_SCAN_FAILED` and repair action pointing to adopt;
- preserve foreign legacy exclusion and chained-origin behavior.

Never infer lineage from the directory name.

---

## Task 6 — preservation verification for cleanup

**Files**
- Modify `candidate_cleanup` / `_cleanup_candidate_locked`
- Extend trusted adapter and tests

### Archive path

Keep existing `archive/candidate-*` semantics and `_require_preserved_head` behavior.

### Protected reachability path

Do not accept `protected_sha` as self-authenticating caller data. The public/tool boundary may accept a branch name to request verification, but the trusted adapter must fresh-query Gitea and construct `ProtectedBranchEvidence` itself.

Verification sequence:

1. trusted Gitea lookup says branch is really protected and returns exact SHA;
2. exact SHA is pinned in `PreservationProof`;
3. candidate HEAD object already exists locally;
4. `git merge-base --is-ancestor HEAD protected_sha` succeeds;
5. delivery branch is absent;
6. registry/reference/cleanliness/root identity guards run as today;
7. immediately before `shutil.rmtree`, fresh-query Gitea again;
8. require branch still protected and exact SHA still equals pinned SHA;
9. rerun ancestor proof, exact candidate HEAD/branch/clean checks, reference guard and delivery-branch absence;
10. only then delete.

Any branch move, protection-state change, unknown lookup, or ancestry failure between steps 1 and 9 leaves the directory intact and fails closed.

### Race test (mandatory)

Inject a verifier whose first call returns protected SHA A and whose second call returns SHA B (or `protected=False`). Assert cleanup fails before `shutil.rmtree` and candidate directory remains.

---

## Task 7 — acceptance fixtures

**Files**
- `tests/test_candidate_lineage_recovery.py`

Build real git fixtures under the same workspace layout production uses.

Requirements:

- derive workspace with `_workspace_root(config_dir)`, never `config_dir.parent`;
- seed `config_dir/projects.yaml` with an existing valid registry root;
- candidate lives under `<workspace>/.mcp-candidate-clones/...`;
- do not register the adopted legacy candidate;
- cleanup must exercise the existing `already_absent=True` registry behavior.

Helpers should create a bare/trusted origin and real feature/main refs so ancestry checks are meaningful.

---

## Task 8 — mandatory scenario 13

Same-repo legacy candidate:

- clean;
- exact current branch/HEAD known;
- HEAD is **not** ancestor of protected `main`;
- exact remote feature branch tip equals HEAD;
- adopt succeeds;
- cleanup using protected-branch reachability fails;
- no archive ref is supplied;
- candidate directory still exists after rejected cleanup.

Assert all fixture preconditions before calling adopt/cleanup so a broken fixture cannot produce a false pass.

---

## Task 9 — mandatory scenario 14 and race variant

Same repo:

- exact expected HEAD;
- trusted Gitea adapter says `main` or `master` is **actually protected**;
- fresh exact protected SHA pinned;
- candidate HEAD is ancestor of that SHA;
- delivery branch absent;
- adopt succeeds;
- explicit cleanup without archive ref succeeds;
- candidate directory removed;
- missing registry entry is handled as `already_absent`.

Then repeat with a verifier that changes protected SHA/protection status between initial proof and pre-delete recheck. Cleanup must fail and leave the directory.

---

## Task 10 — preserve the existing safety matrix

Run/extend regressions for all of:

1. foreign legacy repository exclusion;
2. chained legacy origin;
3. symlinked `.git` rejection;
4. external path/origin rejection;
5. clone-chain cycle;
6. clone-chain max-hop;
7. dirty rejection;
8. active `.ai-bridge` task/delivery rejection;
9. required reference guard;
10. lineage lock;
11. registry CAS / exact root identity;
12. exact head and branch checks;
13. old archive-ref cleanup;
14. legacy v1 tombstone resume in all phases.

Run:

```bash
pytest tests/test_candidate_clone.py tests/test_candidate_lineage_recovery.py -q
ruff check examples/mcp_server tests
mypy examples/mcp_server/candidate_clone.py
pytest -q
```

Update `TODO.md` only after scenarios 13/14 plus the protected-branch race test are green.

---

## Delivery rule

PR-B is not ready to merge unless all of the following are demonstrated in CI:

- adopt succeeds for unmerged same-repo feature HEAD without requiring main reachability;
- cleanup accepts archive proof or **real Gitea-proven protected reachability**, and nothing else;
- protected-branch proof is revalidated immediately before delete;
- v1 tombstones resume under new code;
- reconstructed metadata does not invent historical base provenance;
- all pre-existing candidate safety regressions remain green.
