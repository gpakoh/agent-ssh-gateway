# Legacy Candidate Lineage Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a delivered legacy same-repo candidate recoverable — first by proving its identity and adopting provable lineage metadata, then by permitting explicit cleanup once head preservation is proven — without weakening any existing lineage guard.

**Architecture:** Extend `candidate_clone.py` in place; there is no second lineage implementation. Two new value objects carry the proofs: `IdentityProof` gates the non-destructive adopt path, `PreservationProof` gates the destructive cleanup path. `_find_lineage_claimant` stops letting one unreadable legacy candidate poison unrelated lineages, by reading the candidate's actual branch from git instead of trusting its directory name.

**Tech Stack:** Python 3.11, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-03-tool-surface-parity-and-candidate-lineage-recovery-design.md` (§3.1–§3.5, §3.8–§3.10, §5)

## Global Constraints

- Base branch: `master` at `ccc380ef`. All line references verified against that commit.
- **Adopt must not require the head to be in a protected branch.** It is non-destructive: it never deletes, renames, moves a branch, or touches `HEAD`. Protected-branch reachability is advisory for adopt and mandatory only for cleanup.
- **Cleanup must never run without a `PreservationProof`.** Exactly two constructors are permitted: an `archive/candidate-*` ref, or freshly resolved protected-branch reachability with the exact SHA pinned.
- Reachability is never inferred from the symbolic name `main`/`master`. `_probe_remote_ref` must return `FOUND`, its exact SHA is pinned, and the comparison is against that SHA.
- **Never fetch to establish a preservation claim.** If the head object is not already local, fail closed.
- Identity is derived from git objects and remote configuration only. The directory name is never evidence — which is why `project_id` written by adopt is the candidate's actual directory name.
- Every failure is fail-closed with a specific code. No permissive defaults.
- The injected `reference_guard` stays REQUIRED in every destructive path; never give it a default.
- `_run_git` raises on non-zero exit, so ancestor checks must catch deliberately rather than assume exit 0.
- All 12 existing #430/#434 regression cases must still pass unchanged.

---

### Task 1: `PreservationProof`

**Files:**
- Modify: `examples/mcp_server/candidate_clone.py` (add near `_require_preserved_head`, line 879)
- Test: `tests/test_candidate_lineage_recovery.py` (create)

**Interfaces:**
- Consumes: `_probe_remote_ref` (`:403`), `RemoteRefStatus`, `_run_git` (`:305`), `_local_commit_or_none`, `_fail`.
- Produces:
  - `PreservationProof` frozen dataclass with fields `kind: str`, `preserved_ref: str | None`, `protected_branch: str | None`, `protected_sha: str | None`
  - `PreservationProof.from_archive_ref(preserved_ref: str) -> PreservationProof`
  - `PreservationProof.from_protected_branch_reachability(protected_branch: str, protected_sha: str) -> PreservationProof`
  - `.verify(source_root: Path, head_sha: str, *, context: str) -> None`
  - `.as_identity() -> dict[str, Any]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_candidate_lineage_recovery.py`:

```python
"""Tests for legacy same-repo candidate lineage recovery (adopt + cleanup)."""

from __future__ import annotations

from pathlib import Path

import pytest

from examples.mcp_server.candidate_clone import PreservationProof


class TestArchiveRefProof:
    def test_accepts_archive_ref(self) -> None:
        proof = PreservationProof.from_archive_ref("archive/candidate-abc")
        assert proof.kind == "archive_ref"
        assert proof.preserved_ref == "archive/candidate-abc"

    @pytest.mark.parametrize(
        "bad", ["main", "refs/heads/main", "candidate-abc", "", "archive/other"]
    )
    def test_rejects_non_archive_ref(self, bad: str) -> None:
        with pytest.raises(Exception) as exc:
            PreservationProof.from_archive_ref(bad)
        assert "archive/candidate-" in str(exc.value)


class TestReachabilityProof:
    def test_pins_exact_sha(self) -> None:
        sha = "a" * 40
        proof = PreservationProof.from_protected_branch_reachability("main", sha)
        assert proof.kind == "protected_branch_reachability"
        assert proof.protected_branch == "main"
        assert proof.protected_sha == sha

    def test_rejects_non_sha(self) -> None:
        with pytest.raises(Exception):
            PreservationProof.from_protected_branch_reachability("main", "not-a-sha")

    def test_rejects_sha_of_wrong_length(self) -> None:
        with pytest.raises(Exception):
            PreservationProof.from_protected_branch_reachability("main", "a" * 7)

    def test_verify_fails_closed_when_branch_not_found(self, tmp_path: Path) -> None:
        proof = PreservationProof.from_protected_branch_reachability("main", "a" * 40)
        with pytest.raises(Exception) as exc:
            proof.verify(tmp_path, "b" * 40, context="test")
        assert exc.value.code == "CHECK_FAILED"

    def test_verify_fails_closed_when_head_not_local(self, tmp_path: Path) -> None:
        proof = PreservationProof.from_protected_branch_reachability("main", "a" * 40)
        with pytest.raises(Exception) as exc:
            proof.verify(tmp_path, "b" * 40, context="test")
        assert "not available locally" in str(exc.value) or exc.value.code in {
            "CHECK_FAILED",
            "TOOL_EXECUTION_FAILED",
        }


class TestIdentity:
    def test_archive_identity_is_stable(self) -> None:
        identity = PreservationProof.from_archive_ref("archive/candidate-abc").as_identity()
        assert identity == {
            "kind": "archive_ref",
            "preserved_ref": "archive/candidate-abc",
            "protected_branch": None,
            "protected_sha": None,
        }

    def test_reachability_identity_is_stable(self) -> None:
        identity = PreservationProof.from_protected_branch_reachability(
            "main", "a" * 40
        ).as_identity()
        assert identity == {
            "kind": "protected_branch_reachability",
            "preserved_ref": None,
            "protected_branch": "main",
            "protected_sha": "a" * 40,
        }

    def test_kinds_are_distinct(self) -> None:
        assert (
            PreservationProof.from_archive_ref("archive/candidate-a").kind
            != PreservationProof.from_protected_branch_reachability("main", "a" * 40).kind
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_candidate_lineage_recovery.py -q`
Expected: collection error — `ImportError: cannot import name 'PreservationProof'`

- [ ] **Step 3: Implement `PreservationProof`**

In `examples/mcp_server/candidate_clone.py`, add `from dataclasses import dataclass` to the imports if absent, then insert immediately before `def _require_preserved_head(` (line 879):

```python
@dataclass(frozen=True)
class PreservationProof:
    """Proof that a candidate head survives the removal of its clone.

    Exactly two constructors are permitted, and nothing else may satisfy
    ``candidate_cleanup``:

    ``from_archive_ref``
        The pre-existing contract: an explicit ``archive/candidate-*`` remote
        branch whose SHA equals the candidate head.
    ``from_protected_branch_reachability``
        The candidate head is an ancestor of a freshly resolved protected
        branch. The resolved SHA is pinned at construction and re-validated at
        every use, so a branch that moves after the proof was made invalidates
        the proof instead of silently widening it. No fetch is ever performed:
        a claim must rest on evidence already local.
    """

    kind: str
    preserved_ref: str | None = None
    protected_branch: str | None = None
    protected_sha: str | None = None

    @classmethod
    def from_archive_ref(cls, preserved_ref: str) -> "PreservationProof":
        validated = _validate_ref(preserved_ref)
        if validated is None or not validated.startswith("archive/candidate-"):
            raise _fail(
                "INVALID_INPUT",
                "preserved_ref must be an explicit archive/candidate-* remote branch",
            )
        return cls(kind="archive_ref", preserved_ref=validated)

    @classmethod
    def from_protected_branch_reachability(
        cls,
        protected_branch: str,
        protected_sha: str,
    ) -> "PreservationProof":
        branch = _validate_branch(protected_branch)
        if not isinstance(protected_sha, str) or not re.fullmatch(
            r"[0-9a-fA-F]{40}", protected_sha.strip()
        ):
            raise _fail("INVALID_INPUT", "protected_sha must be a full commit SHA")
        return cls(
            kind="protected_branch_reachability",
            protected_branch=branch,
            protected_sha=protected_sha.strip().lower(),
        )

    def verify(self, source_root: Path, head_sha: str, *, context: str) -> None:
        """Re-prove preservation. Raises unless the head provably survives."""
        if self.kind == "archive_ref":
            probe = _probe_remote_ref(source_root, str(self.preserved_ref))
            if probe.status is not RemoteRefStatus.FOUND or probe.sha != head_sha:
                raise _fail(
                    "CHECK_FAILED",
                    f"candidate head is not proven at the requested preservation ref ({context})",
                    retryable=True,
                )
            return

        if self.kind == "protected_branch_reachability":
            branch = str(self.protected_branch)
            pinned = str(self.protected_sha)
            probe = _probe_remote_ref(source_root, branch)
            if probe.status is not RemoteRefStatus.FOUND:
                raise _fail(
                    "CHECK_FAILED",
                    f"protected branch {branch!r} state is not proven ({context})",
                    retryable=True,
                    details={"probe_status": probe.status.value},
                )
            if probe.sha != pinned:
                raise _fail(
                    "CHECK_FAILED",
                    f"protected branch {branch!r} moved since preservation was pinned ({context})",
                    retryable=True,
                    details={"pinned_sha": pinned, "observed_sha": probe.sha},
                )
            if _local_commit_or_none(source_root, head_sha) is None:
                raise _fail(
                    "CHECK_FAILED",
                    "candidate head is not available locally; preservation cannot be proven without a fetch, which is not permitted",
                    retryable=False,
                    details={"head": head_sha},
                )
            try:
                _run_git(
                    source_root,
                    ["merge-base", "--is-ancestor", head_sha, pinned],
                    operation="prove candidate head reaches protected branch",
                )
            except CandidateCloneError as exc:
                raise _fail(
                    "CHECK_FAILED",
                    "candidate head is not an ancestor of the pinned protected branch",
                    retryable=False,
                    details={"head": head_sha, "protected_sha": pinned},
                ) from exc
            return

        raise _fail(
            "INVALID_INPUT",
            f"unsupported preservation proof kind {self.kind!r}",
        )

    def as_identity(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "preserved_ref": self.preserved_ref,
            "protected_branch": self.protected_branch,
            "protected_sha": self.protected_sha,
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_candidate_lineage_recovery.py -q`
Expected: PASS

- [ ] **Step 5: Run the existing candidate tests to confirm no regression**

Run: `pytest tests/test_candidate_clone.py -q`
Expected: PASS — nothing existing calls the new class yet.

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_server/candidate_clone.py tests/test_candidate_lineage_recovery.py
git commit -m "feat(candidates): add PreservationProof with archive and reachability constructors"
```

---

### Task 2: Route `candidate_cleanup` through `PreservationProof`

**Files:**
- Modify: `examples/mcp_server/candidate_clone.py:1627-1709` (`candidate_cleanup`) and `:1712-1861` (`_cleanup_candidate_locked`)
- Test: `tests/test_candidate_lineage_recovery.py`, `tests/test_candidate_clone.py`

**Interfaces:**
- Consumes: `PreservationProof` (Task 1).
- Produces: `candidate_cleanup(project_id, expected_head_sha, expected_branch, expected_source_project, preserved_ref=None, *, protected_branch=None, protected_sha=None, config_dir, journal_root, reference_guard)`. Exactly one of `preserved_ref` or the `protected_branch`/`protected_sha` pair must be supplied.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_candidate_lineage_recovery.py`:

```python
class TestCleanupPreservationRouting:
    def test_requires_exactly_one_preservation_source(self, tmp_path: Path) -> None:
        from examples.mcp_server.candidate_clone import candidate_cleanup

        common = {
            "project_id": "candidate-x-" + "a" * 12 + "-" + "b" * 12,
            "expected_head_sha": "c" * 40,
            "expected_branch": "feat/x",
            "expected_source_project": "quart-core",
            "config_dir": tmp_path,
            "journal_root": tmp_path / "journal",
            "reference_guard": lambda: None,
        }
        with pytest.raises(Exception) as exc:
            candidate_cleanup(**common)
        assert "preservation" in str(exc.value).lower()

    def test_rejects_both_preservation_sources(self, tmp_path: Path) -> None:
        from examples.mcp_server.candidate_clone import candidate_cleanup

        with pytest.raises(Exception) as exc:
            candidate_cleanup(
                project_id="candidate-x-" + "a" * 12 + "-" + "b" * 12,
                expected_head_sha="c" * 40,
                expected_branch="feat/x",
                expected_source_project="quart-core",
                preserved_ref="archive/candidate-abc",
                protected_branch="main",
                protected_sha="d" * 40,
                config_dir=tmp_path,
                journal_root=tmp_path / "journal",
                reference_guard=lambda: None,
            )
        assert "preservation" in str(exc.value).lower()

    def test_rejects_incomplete_reachability_pair(self, tmp_path: Path) -> None:
        from examples.mcp_server.candidate_clone import candidate_cleanup

        with pytest.raises(Exception):
            candidate_cleanup(
                project_id="candidate-x-" + "a" * 12 + "-" + "b" * 12,
                expected_head_sha="c" * 40,
                expected_branch="feat/x",
                expected_source_project="quart-core",
                protected_branch="main",
                config_dir=tmp_path,
                journal_root=tmp_path / "journal",
                reference_guard=lambda: None,
            )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_candidate_lineage_recovery.py -q -k CleanupPreservationRouting`
Expected: FAIL — `TypeError: candidate_cleanup() got an unexpected keyword argument 'protected_branch'`, and the no-preservation case wrongly raises about `preserved_ref` format rather than about a missing preservation proof.

- [ ] **Step 3: Rewire `candidate_cleanup`**

In `examples/mcp_server/candidate_clone.py`, change the signature of `candidate_cleanup` (line 1627) to make `preserved_ref` optional and add the reachability pair:

```python
def candidate_cleanup(
    project_id: str,
    expected_head_sha: str,
    expected_branch: str,
    expected_source_project: str,
    preserved_ref: str | None = None,
    *,
    protected_branch: str | None = None,
    protected_sha: str | None = None,
    config_dir: Path,
    journal_root: Path,
    reference_guard: Callable[[], None],
) -> CandidateCleanupReceipt:
```

Replace the body block at lines 1661-1669 (the current `preserved_ref` validation) with:

```python
    has_archive = preserved_ref is not None
    has_reachability = protected_branch is not None or protected_sha is not None
    if has_archive and has_reachability:
        raise _fail(
            "INVALID_INPUT",
            "provide exactly one preservation proof, not both an archive ref and protected-branch reachability",
        )
    if not has_archive and not has_reachability:
        raise _fail(
            "INVALID_INPUT",
            "cleanup requires a preservation proof: either preserved_ref (archive/candidate-*) or protected_branch plus protected_sha",
        )
    if has_reachability and (protected_branch is None or protected_sha is None):
        raise _fail(
            "INVALID_INPUT",
            "protected_branch and protected_sha must be supplied together",
        )
    preservation = (
        PreservationProof.from_archive_ref(preserved_ref)
        if has_archive
        else PreservationProof.from_protected_branch_reachability(
            str(protected_branch),
            str(protected_sha),
        )
    )
```

Replace `expected_identity` (lines 1681-1689) so it carries the normalized proof rather than a raw ref:

```python
    expected_identity = {
        "version": 1,
        "project_id": project_id,
        "source_project": expected_source_project,
        "branch": expected_branch,
        "head": expected_head_sha,
        "preservation": preservation.as_identity(),
        "registry_root": expected_registry_root,
    }
```

Pass `preservation=preservation` into the `_cleanup_candidate_locked` call (after `preserved_ref=preserved_ref,`).

`expected_identity` is not compared against clone metadata — it is compared
against the **cleanup tombstone** (`_cleanup_candidate_locked`, the loop over
`expected_identity.items()`), and it seeds newly written tombstones. So renaming
`preserved_ref` to `preservation` changes the on-disk tombstone shape: a
tombstone written by the current code will mismatch after deploy and raise
`WORKSPACE_CONTENDED` ("tombstone identity mismatch") forever, because nothing
clears it. That is fail-closed and therefore safe, but it strands any cleanup
that was interrupted mid-flight across the deploy. Call it out in the PR
description, and confirm with the owner whether any tombstone is currently in
`prepared` or `registry_removed` before shipping:

```bash
find <journal_root> -name '*candidate*' -path '*tombstone*' -print
```

If any exist, finish or abandon them on the old code first; do not deploy over an
in-flight cleanup.

- [ ] **Step 4: Rewire `_cleanup_candidate_locked`**

Change its signature: replace `preserved_ref: str,` with `preservation: PreservationProof,`.

Replace both `_require_preserved_head(...)` call sites — at line 1739 (before the existence check) and line 1837 (before filesystem removal) — with:

```python
    preservation.verify(source_root, expected_head_sha, context="before existence check")
```

and:

```python
        preservation.verify(
            source_root,
            expected_head_sha,
            context="before filesystem removal",
        )
```

Replace the two `preserved_ref=preserved_ref,` arguments in the `CandidateCleanupReceipt(...)` constructions (lines 1763 and 1790) with:

```python
            preserved_ref=preservation.preserved_ref,
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_candidate_lineage_recovery.py tests/test_candidate_clone.py -q`
Expected: PASS. Existing archive-ref cleanup tests must still pass unchanged — that is the backward-compatibility proof for Task 2.

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_server/candidate_clone.py tests/test_candidate_lineage_recovery.py
git commit -m "feat(candidates): route cleanup through an explicit PreservationProof"
```

---

### Task 3: `IdentityProof` and the adopt path

**Files:**
- Modify: `examples/mcp_server/candidate_clone.py` (add after `_legacy_trusted_remote`, line 1237)
- Test: `tests/test_candidate_lineage_recovery.py`

**Interfaces:**
- Consumes: `_validate_candidate_root` (`:805`), `_reject_symlinked_git_dir` (`:821`), `_legacy_trusted_remote` (`:1213`), `_resolve_trusted_remote`, `_status_state` (`:469`), `_read_metadata`, `_write_metadata` (`:569`), `_candidate_has_active_evidence` (`:794`), `_lineage_lock`.
- Produces:
  - `IdentityProof` frozen dataclass: `source_project`, `branch`, `head_sha`, `candidate_dir_name`, `advisory_preservation: dict | None`
  - `prove_candidate_identity(candidate_root, candidates_root, source_root, *, expected_head_sha, expected_branch, expected_source_project) -> IdentityProof`
  - `adopt_candidate_lineage(project_id, expected_head_sha, expected_branch, expected_source_project, *, config_dir, journal_root, reference_guard) -> CandidateAdoptReceipt`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_candidate_lineage_recovery.py`:

```python
class TestAdoptIdentityProof:
    def test_proof_module_exposes_entry_points(self) -> None:
        from examples.mcp_server import candidate_clone

        assert hasattr(candidate_clone, "prove_candidate_identity")
        assert hasattr(candidate_clone, "adopt_candidate_lineage")

    def test_adopt_never_requires_protected_reachability(self) -> None:
        """Adopt is non-destructive, so no preservation proof is a precondition."""
        import inspect

        from examples.mcp_server import candidate_clone

        params = inspect.signature(candidate_clone.adopt_candidate_lineage).parameters
        assert "protected_branch" not in params
        assert "protected_sha" not in params
        assert "preserved_ref" not in params

    def test_adopt_does_not_delete_anything(self) -> None:
        import inspect

        from examples.mcp_server import candidate_clone

        src = inspect.getsource(candidate_clone.adopt_candidate_lineage)
        assert "shutil.rmtree" not in src
        assert "unlink" not in src
        assert "rename" not in src

    def test_identity_proof_requires_exact_head(self) -> None:
        import inspect

        from examples.mcp_server import candidate_clone

        src = inspect.getsource(candidate_clone.prove_candidate_identity)
        assert "rev-parse" in src
        assert "merge-base" not in src, (
            "identity proof must not depend on protected-branch reachability"
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_candidate_lineage_recovery.py -q -k AdoptIdentityProof`
Expected: FAIL — `prove_candidate_identity` does not exist

- [ ] **Step 3: Implement `IdentityProof` and `prove_candidate_identity`**

Insert after `_legacy_trusted_remote` (line 1237):

```python
@dataclass(frozen=True)
class IdentityProof:
    """Provable identity of a legacy candidate, sufficient for adopt.

    Deliberately carries no preservation requirement. Adopt rewrites metadata
    and nothing else, so no data can be lost; requiring the head to be in a
    protected branch would block exactly the live unmerged candidates that
    recovery exists to legalize.
    """

    source_project: str
    branch: str
    head_sha: str
    candidate_dir_name: str
    advisory_preservation: dict[str, Any] | None = None


def _read_current_branch(candidate_root: Path) -> str:
    return _run_git(
        candidate_root,
        ["rev-parse", "--abbrev-ref", "HEAD"],
        operation="read candidate branch",
    )


def prove_candidate_identity(
    candidate_root: Path,
    candidates_root: Path,
    source_root: Path,
    *,
    expected_head_sha: str,
    expected_branch: str,
    expected_source_project: str,
) -> IdentityProof:
    """Prove a legacy candidate's identity from git objects and remotes only.

    Every condition is fail-closed. The directory name is never consulted as
    evidence of identity.
    """
    _validate_candidate_root(candidate_root, candidates_root)
    _reject_symlinked_git_dir(candidate_root)

    requested_url, _ = _resolve_trusted_remote(source_root)
    candidate_url = _legacy_trusted_remote(candidate_root)
    if not (requested_url and candidate_url):
        raise _fail(
            "CANDIDATE_LINEAGE_SCAN_FAILED",
            "candidate trusted repository identity could not be resolved",
        )
    if requested_url != candidate_url:
        raise _fail(
            "CANDIDATE_LINEAGE_SCAN_FAILED",
            "candidate belongs to a different trusted repository than the source root",
            details={"candidate_project_id": candidate_root.name},
        )

    current_branch = _read_current_branch(candidate_root)
    if current_branch in {"", "HEAD"}:
        raise _fail(
            "CANDIDATE_LINEAGE_SCAN_FAILED",
            "candidate is in a detached HEAD state and its branch identity is unprovable",
            details={"candidate_project_id": candidate_root.name},
        )
    if current_branch != expected_branch:
        raise _fail(
            "CANDIDATE_LINEAGE_SCAN_FAILED",
            "candidate branch does not match the expected branch",
            details={
                "candidate_project_id": candidate_root.name,
                "branch": current_branch,
                "expected_branch": expected_branch,
            },
        )

    current_head = _run_git(
        candidate_root,
        ["rev-parse", "HEAD"],
        operation="read candidate head",
    ).lower()
    if current_head != expected_head_sha:
        raise _fail(
            "CANDIDATE_LINEAGE_SCAN_FAILED",
            "candidate HEAD does not match expected_head_sha",
            details={
                "candidate_project_id": candidate_root.name,
                "head": current_head,
            },
        )

    dirty, _status_sha, _status_entries = _status_state(candidate_root)
    if dirty:
        raise _fail(
            "WORKSPACE_CONTENDED",
            "candidate worktree is dirty and cannot be adopted",
            details={"candidate_project_id": candidate_root.name},
        )

    existing: dict[str, Any] = {}
    try:
        existing = _read_metadata(candidate_root)
    except Exception:
        existing = {}
    proposed = {
        "version": 1,
        "project_id": candidate_root.name,
        "source_project": expected_source_project,
        "branch": expected_branch,
        "head": expected_head_sha,
        "root": f".mcp-candidate-clones/{candidate_root.name}",
    }
    if existing:
        conflicting = {
            key: {"existing": existing.get(key), "proposed": value}
            for key, value in proposed.items()
            if key in existing and existing.get(key) != value
        }
        if conflicting:
            raise _fail(
                "WORKSPACE_CONTENDED",
                "candidate already carries conflicting managed metadata; refusing to overwrite",
                details={"conflicting_fields": sorted(conflicting)},
            )

    if _candidate_has_active_evidence(candidate_root):
        raise _fail(
            "WORKSPACE_CONTENDED",
            "candidate still has active task or delivery evidence and cannot be adopted",
            details={"candidate_project_id": candidate_root.name},
        )

    advisory = _advisory_preservation(candidate_root, source_root, expected_head_sha)

    return IdentityProof(
        source_project=expected_source_project,
        branch=expected_branch,
        head_sha=expected_head_sha,
        candidate_dir_name=candidate_root.name,
        advisory_preservation=advisory,
    )


def _advisory_preservation(
    candidate_root: Path,
    source_root: Path,
    head_sha: str,
) -> dict[str, Any] | None:
    """Best-effort, never-blocking record of how the head is preserved.

    Recorded so a later cleanup can reuse an already-proven proof. Absence of a
    result is not an error: for the non-destructive adopt path the candidate
    directory is itself the preservation.
    """
    for branch in ("main", "master"):
        try:
            probe = _probe_remote_ref(source_root, branch)
        except Exception:
            continue
        if probe.status is not RemoteRefStatus.FOUND:
            continue
        if _local_commit_or_none(source_root, head_sha) is None:
            continue
        try:
            _run_git(
                source_root,
                ["merge-base", "--is-ancestor", head_sha, probe.sha],
                operation="record advisory preservation",
            )
        except Exception:
            continue
        return {
            "kind": "protected_branch_reachability",
            "protected_branch": branch,
            "protected_sha": probe.sha,
        }
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_candidate_lineage_recovery.py -q -k AdoptIdentityProof`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/candidate_clone.py tests/test_candidate_lineage_recovery.py
git commit -m "feat(candidates): add IdentityProof derived from git objects only"
```

---

### Task 4: `adopt_candidate_lineage`

**Files:**
- Modify: `examples/mcp_server/candidate_clone.py` (add after `prove_candidate_identity`)
- Test: `tests/test_candidate_lineage_recovery.py`

**Interfaces:**
- Consumes: `prove_candidate_identity` (Task 3), `_write_metadata` (`:569`), `_lineage_lock`, `_enforce_reference_guard` (`:915`), `_workspace_root`, `_source_root`, `_candidate_clones_root`, `_project_id` (`:556`).
- Produces: `CandidateAdoptReceipt` dataclass (`project_id`, `source_project`, `branch`, `head`, `already_adopted`, `advisory_preservation`) and `adopt_candidate_lineage(...)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_candidate_lineage_recovery.py`:

```python
class TestAdoptIsNonDestructive:
    def test_adopt_writes_metadata_only(self, legacy_same_repo_candidate) -> None:
        from examples.mcp_server.candidate_clone import adopt_candidate_lineage

        candidate_dir = legacy_same_repo_candidate.path
        head = _git(candidate_dir, "rev-parse", "HEAD")
        branch = _git(candidate_dir, "rev-parse", "--abbrev-ref", "HEAD")
        before = sorted(p.name for p in candidate_dir.iterdir())

        receipt = adopt_candidate_lineage(
            candidate_dir.name,
            head,
            branch,
            "quart-core",
            config_dir=legacy_same_repo_candidate.config_dir,
            journal_root=legacy_same_repo_candidate.journal_root,
            reference_guard=lambda: None,
        )

        assert receipt.already_adopted is False
        assert candidate_dir.exists()
        assert _git(candidate_dir, "rev-parse", "HEAD") == head
        assert sorted(p.name for p in candidate_dir.iterdir()) == before

    def test_adopt_is_idempotent(self, legacy_same_repo_candidate) -> None:
        from examples.mcp_server.candidate_clone import adopt_candidate_lineage

        candidate_dir = legacy_same_repo_candidate.path
        head = _git(candidate_dir, "rev-parse", "HEAD")
        branch = _git(candidate_dir, "rev-parse", "--abbrev-ref", "HEAD")
        kwargs = {
            "config_dir": legacy_same_repo_candidate.config_dir,
            "journal_root": legacy_same_repo_candidate.journal_root,
            "reference_guard": lambda: None,
        }
        adopt_candidate_lineage(candidate_dir.name, head, branch, "quart-core", **kwargs)
        again = adopt_candidate_lineage(
            candidate_dir.name, head, branch, "quart-core", **kwargs
        )
        assert again.already_adopted is True

    def test_adopt_rejects_wrong_expected_head(self, legacy_same_repo_candidate) -> None:
        from examples.mcp_server.candidate_clone import adopt_candidate_lineage

        candidate_dir = legacy_same_repo_candidate.path
        branch = _git(candidate_dir, "rev-parse", "--abbrev-ref", "HEAD")
        with pytest.raises(Exception) as exc:
            adopt_candidate_lineage(
                candidate_dir.name,
                "0" * 40,
                branch,
                "quart-core",
                config_dir=legacy_same_repo_candidate.config_dir,
                journal_root=legacy_same_repo_candidate.journal_root,
                reference_guard=lambda: None,
            )
        assert exc.value.code == "CANDIDATE_LINEAGE_SCAN_FAILED"
```

Add the shared fixture and helper used above, at the top of the test file after the imports:

```python
import subprocess  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=True,
    )
    return out.stdout.strip()
```

and a `legacy_same_repo_candidate` fixture that builds a real bare origin plus a
legacy-named clone in the same repository, with **no** managed lineage metadata.
It yields a small frozen dataclass so the tests read cleanly:

```python
@dataclass(frozen=True)
class LegacyCandidate:
    path: Path          # the legacy-named clone directory itself
    config_dir: Path    # agent config dir, inside the temp workspace
    journal_root: Path
    source_root: Path   # the bare origin
```

The workspace root is **not** stored: tests derive it with
`_workspace_root(legacy.config_dir)`, exactly as production code does. Do not
assume `config_dir.parent` is the workspace root -- `_candidate_clones_root`
resolves against the *workspace*, so passing the wrong root makes the fixture
invisible to the scanner.

The fixture must also seed a real registry, because `candidate_cleanup` reaches
`_load_registry`, which raises `TOOL_EXECUTION_FAILED` when
`config_dir/projects.yaml` is missing or its default root is not an existing
directory. Both acceptance cases would then fail for a reason unrelated to the
feature under test:

```python
(config_dir / "projects.yaml").write_text(
    "version: 1\n"
    f"registry_root: {workspace_root}\n\n"
    "projects:\n"
    "{}\n",
    encoding="utf-8",
)
```

Follow the shape used by the `registry_layout` fixture in
`tests/test_mcp_project_registration.py`.

Do **not** register the adopted candidate in that registry. That is the point of
the recovery: an adopted candidate has metadata but no registry entry, and
`_unregister_project_exact_unlocked` returns `already_absent=True` for a missing
entry rather than raising, which is what lets cleanup of an adopted candidate
succeed.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_candidate_lineage_recovery.py -q -k AdoptIsNonDestructive`
Expected: FAIL — `adopt_candidate_lineage` does not exist

- [ ] **Step 3: Implement `adopt_candidate_lineage`**

Add the receipt dataclass and function after `prove_candidate_identity`:

```python
@dataclass(frozen=True)
class CandidateAdoptReceipt:
    project_id: str
    source_project: str
    branch: str
    head: str
    already_adopted: bool
    advisory_preservation: dict[str, Any] | None = None


def adopt_candidate_lineage(
    project_id: str,
    expected_head_sha: str,
    expected_branch: str,
    expected_source_project: str,
    *,
    config_dir: Path,
    journal_root: Path,
    reference_guard: Callable[[], None],
) -> CandidateAdoptReceipt:
    """Reconstruct provable lineage metadata for a legacy candidate.

    Non-destructive and idempotent. Never deletes, renames, or moves anything,
    and never touches HEAD. Takes no preservation proof because it performs no
    destructive step; the candidate directory is itself the preservation.

    The written ``project_id`` is the candidate's actual directory name, not a
    synthesised managed id, because ``_read_candidate_metadata`` requires the two
    to match and renaming would be a move.
    """
    if not isinstance(project_id, str) or not project_id.startswith("candidate-"):
        raise _fail("INVALID_INPUT", "project_id must be a candidate directory name")
    if not isinstance(expected_head_sha, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", expected_head_sha.strip()
    ):
        raise _fail("INVALID_INPUT", "expected_head_sha must be a full commit SHA")
    expected_head_sha = expected_head_sha.strip().lower()
    expected_branch = _validate_branch(expected_branch)
    if not isinstance(expected_source_project, str) or not expected_source_project.strip():
        raise _fail("INVALID_INPUT", "expected_source_project must be non-empty")
    expected_source_project = expected_source_project.strip()
    if not callable(reference_guard):
        raise _fail("INVALID_INPUT", "reference_guard must be callable and is required")

    config_dir = config_dir.resolve()
    journal_root = journal_root.resolve()
    workspace_root = _workspace_root(config_dir)
    source_root = _source_root(
        config_dir, expected_source_project, workspace_root=workspace_root
    )
    candidates_root = _candidate_clones_root(workspace_root)
    candidate_root = candidates_root / project_id

    with _lineage_lock(workspace_root, expected_source_project, expected_branch):
        if not candidate_root.exists():
            raise _fail("PROJECT_NOT_FOUND", "candidate clone does not exist")

        existing: dict[str, Any] = {}
        try:
            existing = _read_metadata(candidate_root)
        except Exception:
            existing = {}
        if (
            existing.get("project_id") == project_id
            and existing.get("source_project") == expected_source_project
            and existing.get("branch") == expected_branch
            and existing.get("head") == expected_head_sha
        ):
            return CandidateAdoptReceipt(
                project_id=project_id,
                source_project=expected_source_project,
                branch=expected_branch,
                head=expected_head_sha,
                already_adopted=True,
                advisory_preservation=existing.get("advisory_preservation"),
            )

        proof = prove_candidate_identity(
            candidate_root,
            candidates_root,
            source_root,
            expected_head_sha=expected_head_sha,
            expected_branch=expected_branch,
            expected_source_project=expected_source_project,
        )

        _enforce_reference_guard(reference_guard)

        # NB: _write_metadata takes the metadata FILE path, not the candidate
        # root -- it writes <path>.tmp then replaces <path>. Passing the root
        # would create a file where the clone directory belongs.
        _write_metadata(
            _metadata_path(candidate_root),
            {
                "version": 1,
                "project_id": proof.candidate_dir_name,
                "source_project": proof.source_project,
                "branch": proof.branch,
                "head": proof.head_sha,
                "root": f".mcp-candidate-clones/{proof.candidate_dir_name}",
                "lineage_reconstructed": True,
                "advisory_preservation": proof.advisory_preservation,
            },
        )

        return CandidateAdoptReceipt(
            project_id=proof.candidate_dir_name,
            source_project=proof.source_project,
            branch=proof.branch,
            head=proof.head_sha,
            already_adopted=False,
            advisory_preservation=proof.advisory_preservation,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_candidate_lineage_recovery.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/candidate_clone.py tests/test_candidate_lineage_recovery.py
git commit -m "feat(candidates): add non-destructive idempotent lineage adopt"
```

---

### Task 5: Unblock unrelated lineages in `_find_lineage_claimant`

**Files:**
- Modify: `examples/mcp_server/candidate_clone.py:1288-1297` (`_find_lineage_claimant`)
- Test: `tests/test_candidate_lineage_recovery.py`

**Interfaces:**
- Consumes: `_read_current_branch` (Task 3), `_legacy_trusted_remote` (`:1213`), `_resolve_trusted_remote`.
- Produces: no signature change. Behaviour: an unreadable legacy candidate on a *different* branch is skipped instead of aborting the scan.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_candidate_lineage_recovery.py`:

```python
class TestScanScoping:
    def test_unrelated_legacy_candidate_does_not_poison_scan(
        self, legacy_same_repo_candidate
    ) -> None:
        from examples.mcp_server.candidate_clone import (
            _find_lineage_claimant,
            _workspace_root,
        )

        candidate_dir = legacy_same_repo_candidate.path
        result = _find_lineage_claimant(
            _workspace_root(legacy_same_repo_candidate.config_dir),
            source_project="quart-core",
            branch="feat/some-other-branch",
            exclude_project_id="candidate-none",
            source_root=legacy_same_repo_candidate.source_root,
        )
        assert result is None

    def test_same_branch_legacy_candidate_still_fails_closed(
        self, legacy_same_repo_candidate
    ) -> None:
        from examples.mcp_server.candidate_clone import (
            _find_lineage_claimant,
            _workspace_root,
        )

        candidate_dir = legacy_same_repo_candidate.path
        branch = _git(candidate_dir, "rev-parse", "--abbrev-ref", "HEAD")
        with pytest.raises(Exception) as exc:
            _find_lineage_claimant(
                _workspace_root(legacy_same_repo_candidate.config_dir),
                source_project="quart-core",
                branch=branch,
                exclude_project_id="candidate-none",
                source_root=legacy_same_repo_candidate.source_root,
            )
        assert exc.value.code == "CANDIDATE_LINEAGE_SCAN_FAILED"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_candidate_lineage_recovery.py -q -k ScanScoping`
Expected: FAIL — `test_unrelated_legacy_candidate_does_not_poison_scan` raises `CANDIDATE_LINEAGE_SCAN_FAILED` instead of returning `None`.

- [ ] **Step 3: Implement branch-scoped skipping**

In `_find_lineage_claimant`, replace the metadata read block (lines 1288-1297):

```python
                try:
                    metadata = _read_candidate_metadata(Path(entry.path))
                except CandidateCloneError as exc:
                    # An unreadable legacy candidate must not abort the scan for
                    # unrelated lineages -- but it also must not be silently
                    # ignored while it could still claim this one. Decide from
                    # git objects, never from the directory name: a different
                    # branch cannot claim this lineage, a matching branch might.
                    if suffix is None:
                        try:
                            candidate_branch = _read_current_branch(Path(entry.path))
                        except CandidateCloneError:
                            candidate_branch = ""
                        if candidate_branch and candidate_branch != branch:
                            continue
                    exc.details = {
                        "candidate_project_id": entry.name,
                        "repair_action": "Adopt this candidate with candidate_adopt_lineage, then clean it up with candidate_cleanup and an explicit preservation proof; do not delete or rewrite lineage metadata blindly.",
                    }
                    raise
                if metadata.get("source_project") == source_project and metadata.get("branch") == branch:
                    return metadata
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_candidate_lineage_recovery.py tests/test_candidate_clone.py -q`
Expected: PASS — including all 12 pre-existing lineage regression cases, which must be untouched.

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/candidate_clone.py tests/test_candidate_lineage_recovery.py
git commit -m "fix(candidates): scope unreadable legacy candidates to their own branch"
```

---

### Task 6: Expose adopt as an MCP tool

**Files:**
- Modify: `examples/mcp_server/candidate_clone.py` (registration block near line 1894)
- Test: `tests/test_candidate_lineage_recovery.py`

**Interfaces:**
- Consumes: `adopt_candidate_lineage` (Task 4).
- Produces: tool `candidate_adopt_lineage` accepting `project_id`, `expected_head_sha`, `expected_branch`, `expected_source_project`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_candidate_lineage_recovery.py`:

```python
class TestAdoptToolRegistration:
    def test_tool_is_registered(self) -> None:
        from examples.mcp_server import candidate_clone

        assert "candidate_adopt_lineage" in getattr(
            candidate_clone, "REGISTERED_TOOLS", candidate_clone.__all__
        )

    def test_tool_takes_no_preservation_argument(self) -> None:
        from examples.mcp_server import candidate_clone

        entry = candidate_clone.REGISTERED_TOOLS["candidate_adopt_lineage"]
        assert "protected_branch" not in entry
        assert "protected_sha" not in entry
        assert "preserved_ref" not in entry
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_candidate_lineage_recovery.py -q -k AdoptToolRegistration`
Expected: FAIL — `candidate_adopt_lineage` is not registered

- [ ] **Step 3: Register the tool**

Follow the exact registration pattern already used for `candidate_cleanup` in the same file (around line 1894): add `candidate_adopt_lineage` to the module's tool registry with parameters `project_id`, `expected_head_sha`, `expected_branch`, `expected_source_project`, wiring `config_dir`, `journal_root`, and the existing injected `reference_guard`, and returning `tool_success`/`tool_error` exactly as `candidate_cleanup` does.

Its docstring must state that it is non-destructive, idempotent, requires no preservation proof, and writes `project_id` equal to the candidate's directory name.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_candidate_lineage_recovery.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/candidate_clone.py tests/test_candidate_lineage_recovery.py
git commit -m "feat(candidates): expose candidate_adopt_lineage MCP tool"
```

---

### Task 7: The two acceptance cases, lint, and TODO

**Files:**
- Modify: `tests/test_candidate_lineage_recovery.py`
- Modify: `TODO.md`

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: no new interfaces.

- [ ] **Step 1: Write the acceptance-case tests**

Append to `tests/test_candidate_lineage_recovery.py`:

```python
class TestAcceptanceCases:
    def test_case_13_adopt_allowed_cleanup_forbidden_when_head_not_in_main(
        self, legacy_same_repo_candidate_unmerged
    ) -> None:
        """Spec section 5, case 13.

        A clean same-repo legacy candidate whose head is NOT in main, but whose
        exact remote feature branch preserves it: adopt is allowed, cleanup is
        forbidden. This is the case that a main-reachability-first design would
        wrongly block.
        """
        from examples.mcp_server.candidate_clone import (
            adopt_candidate_lineage,
            candidate_cleanup,
        )

        candidate_dir = legacy_same_repo_candidate_unmerged.path
        head = _git(candidate_dir, "rev-parse", "HEAD")
        branch = _git(candidate_dir, "rev-parse", "--abbrev-ref", "HEAD")
        src = legacy_same_repo_candidate_unmerged.source_root

        # Preconditions: not merged into main, but preserved by a remote feature
        # branch. Both must hold or this case proves nothing.
        feature_branch = _FEATURE_BRANCH
        assert not _is_ancestor(src, head, _remote_sha(src, "main"))
        assert _remote_sha(src, feature_branch) == head

        adopted = adopt_candidate_lineage(
            candidate_dir.name,
            head,
            branch,
            "quart-core",
            config_dir=legacy_same_repo_candidate_unmerged.config_dir,
            journal_root=legacy_same_repo_candidate_unmerged.journal_root,
            reference_guard=lambda: None,
        )
        assert adopted.already_adopted is False
        assert candidate_dir.exists()

        with pytest.raises(Exception) as exc:
            candidate_cleanup(
                candidate_dir.name,
                head,
                branch,
                "quart-core",
                protected_branch="main",
                protected_sha=_remote_sha(legacy_same_repo_candidate_unmerged.source_root, "main"),
                config_dir=legacy_same_repo_candidate_unmerged.config_dir,
                journal_root=legacy_same_repo_candidate_unmerged.journal_root,
                reference_guard=lambda: None,
            )
        assert exc.value.code in {"CHECK_FAILED", "WORKSPACE_CONTENDED"}
        assert candidate_dir.exists(), "cleanup must not remove an unpreserved head"

    def test_case_14_adopt_and_cleanup_allowed_via_reachability(
        self, legacy_same_repo_candidate
    ) -> None:
        """Spec section 5, case 14.

        A legacy candidate whose head is already reachable from a freshly
        resolved protected branch: adopt AND explicit cleanup are allowed with no
        archive ref at all.
        """
        from examples.mcp_server.candidate_clone import (
            adopt_candidate_lineage,
            candidate_cleanup,
        )

        candidate_dir = legacy_same_repo_candidate.path
        head = _git(candidate_dir, "rev-parse", "HEAD")
        branch = _git(candidate_dir, "rev-parse", "--abbrev-ref", "HEAD")
        src = legacy_same_repo_candidate.source_root
        main_sha = _remote_sha(src, "main")
        assert _is_ancestor(src, head, main_sha), "precondition: head is in main"

        adopt_candidate_lineage(
            candidate_dir.name,
            head,
            branch,
            "quart-core",
            config_dir=legacy_same_repo_candidate.config_dir,
            journal_root=legacy_same_repo_candidate.journal_root,
            reference_guard=lambda: None,
        )

        receipt = candidate_cleanup(
            candidate_dir.name,
            head,
            branch,
            "quart-core",
            protected_branch="main",
            protected_sha=main_sha,
            config_dir=legacy_same_repo_candidate.config_dir,
            journal_root=legacy_same_repo_candidate.journal_root,
            reference_guard=lambda: None,
        )
        assert receipt.directory_removed is True
        assert not candidate_dir.exists()
```

Add these read-only git helpers, plus the `legacy_same_repo_candidate_unmerged`
fixture whose head is **not** an ancestor of `main` but **is** the tip of a remote
feature branch (case 13 depends on both halves of that):

```python
# The unmerged fixture's head sits on this remote feature branch, never on main.
_FEATURE_BRANCH = "feat/unmerged-head"


def _remote_sha(source_root: Path, branch: str) -> str:
    """Resolve a remote branch SHA via ls-remote. Mutates nothing."""
    out = subprocess.run(
        ["git", "ls-remote", "--heads", str(source_root), f"refs/heads/{branch}"],
        text=True,
        capture_output=True,
        check=True,
    )
    line = out.stdout.strip()
    assert line, f"remote branch {branch!r} does not exist"
    return line.split()[0]


def _is_ancestor(source_root: Path, ancestor: str, descendant: str) -> bool:
    """merge-base --is-ancestor exits 1 when NOT an ancestor, so no check=True."""
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(source_root),
            capture_output=True,
        ).returncode
        == 0
    )
```

Each acceptance test must assert its own precondition before acting, otherwise a
buggy fixture makes the case pass for the wrong reason:

- case 13: `assert not _is_ancestor(src, head, _remote_sha(src, "main"))`
- case 14: `assert _is_ancestor(src, head, _remote_sha(src, "main"))`

Case 13 must additionally assert the saving grace still exists, so that a future
"adopt implies preservation" change cannot silently delete the head:

```python
feature_ref = _remote_sha(src, feature_branch)
assert feature_ref == head  # exact remote feature branch preserves the head
```

- [ ] **Step 2: Run the acceptance tests**

Run: `pytest tests/test_candidate_lineage_recovery.py -q -k AcceptanceCases`
Expected: PASS. Case 13 asserting `candidate_dir.exists()` after the rejected
cleanup is the load-bearing check — it proves recovery did not become a deletion
path.

- [ ] **Step 3: Run the full candidate suite**

Run: `pytest tests/test_candidate_clone.py tests/test_candidate_lineage_recovery.py -q`
Expected: PASS — the 12 #430/#434 regression cases plus the new ones.

- [ ] **Step 4: Run Ruff and mypy**

Run: `ruff format examples/mcp_server/candidate_clone.py tests/test_candidate_lineage_recovery.py && ruff check examples/mcp_server tests && mypy examples/mcp_server/candidate_clone.py`
Expected: no errors.

- [ ] **Step 5: Run the whole suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 6: Update TODO.md**

Add or close the separate same-repo recovery entry, stating both acceptance cases
by number so the closure is auditable:

```markdown
- [x] P1: Legacy same-repo candidate recovery — `candidate_adopt_lineage`
      (non-destructive, idempotent, no preservation precondition) plus
      `PreservationProof.from_protected_branch_reachability` for cleanup.
      Verified by acceptance cases 13 (adopt allowed / cleanup forbidden when
      the head is not in main) and 14 (adopt + cleanup without an archive ref
      when the head reaches a freshly resolved protected branch).
```

- [ ] **Step 7: Commit**

```bash
git add TODO.md tests/test_candidate_lineage_recovery.py
git commit -m "test(candidates): cover acceptance cases 13 and 14; close recovery TODO"
```

---

## Post-merge: PR-C live verification

Not code. After merging and deploying, run spec §7 against the real Gateway. The
legacy candidate in production is
`candidate-quart-core-feat-supervisor-read-knowledge-capabilities-20261001`; adopt
it first, then clean it up with an explicit reachability preservation proof. Do
not modify or merge `quart-core #227` itself.

## Self-Review

**1. Spec coverage.** §3.5 PreservationProof → Task 1. §3.10 cleanup routing →
Task 2. §3.3 IdentityProof → Task 3. §3.9 adopt semantics (non-destructive,
idempotent, `project_id` = directory name, no rename) → Task 4. §3.8 scan scoping
→ Task 5. §3.6/§3.4 advisory preservation → Task 3's `_advisory_preservation`.
§5 cases 13 and 14 → Task 7. §6 TODO → Task 7. The 12 existing regression cases
are asserted unchanged by the Task 2/5/7 suite runs.

**2. Placeholder scan.** Task 6 Step 3 and Task 7 Step 1 describe fixtures and
registration "following the existing pattern" rather than transcribing them. That
is deliberate: both copy an established, verified pattern from
`candidate_clone.py` (`candidate_cleanup` at line 1627 and the registry at line
1894) rather than inventing a new one, and the required behaviour is pinned by
assertions. Every other code step carries literal code.

**3. Type consistency.** `PreservationProof.from_archive_ref` and
`.from_protected_branch_reachability` are defined in Task 1 and called with the
same names in Tasks 2, 4, and 7. `prove_candidate_identity(candidate_root,
candidates_root, source_root, *, expected_head_sha, expected_branch,
expected_source_project)` is called from Task 4 with that exact order.
`adopt_candidate_lineage(project_id, expected_head_sha, expected_branch,
expected_source_project, *, config_dir, journal_root, reference_guard)` is called
identically in Tasks 4, 6, and 7. `CandidateAdoptReceipt.already_adopted` and
`.advisory_preservation` match between Task 4 and Task 7's assertions.
`CandidateCleanupReceipt.directory_removed` is asserted in Task 7 and exists in
the current code at line 1764. `_read_current_branch` is introduced in Task 3 and
used in Task 5.