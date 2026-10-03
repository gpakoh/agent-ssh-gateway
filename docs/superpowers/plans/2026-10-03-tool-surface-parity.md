# Tool-Surface Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the external MCP tool-surface discrepancy measurable, and fail closed only where a mutation's compare-and-swap precondition is provably unsatisfiable on the calling client's surface.

**Architecture:** A new `surface_parity.py` module owns one responsibility — storing explicit client-supplied observations of externally visible tool names, binding each observation to the submitting identity plus the current toolset hash, deriving guard coverage from a bound observation, and gating mutations on *proven* absence only. `tools_manifest.py` renders the contract; `server.py` accepts and records the observation; `remote.py` consults the gate. No component infers external visibility — the server cannot observe a connector's catalog, so visibility is always client-supplied and labelled unverified until bound.

**Tech Stack:** Python 3.11, FastMCP, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-03-tool-surface-parity-and-candidate-lineage-recovery-design.md` (§1.5, §2.1–§2.6)

## Global Constraints

- Base branch: `master` at `ccc380ef`. All line references below are verified against that commit.
- **Never** assert external tool visibility derived from the server manifest. The server cannot observe a connector catalog; `tools/list` is served in full and what the client retains is invisible.
- Observation completeness (`client_visible_tool_names_complete`) **defaults to `false`**. A name missing from an incomplete report is *unknown*, never *absent*.
- The gate fires only when a **bound**, **complete** observation lacks a **specific** guard dependency. No observation, a stale `toolset_hash`, a foreign identity, or an incomplete report must never block a mutation.
- `guard_coverage` is computed by the Gateway. A caller may never submit a coverage claim.
- Reuse `compute_toolset_hash` (`mcp_infra/tool_registry.py:157`). Do not add a second fingerprint mechanism.
- Reuse `_current_auth_reuse_key()` (`server.py:171`). The raw bearer token must never enter adapter code, the observation store, logs, or errors.
- Every new `tool_error` code must be added to `ERROR_CODES` (`tool_results.py:23`) or it is silently downgraded to `INTERNAL_ERROR` (`tool_results.py:233`).
- The verified guard-dependency map is exactly:
  ```python
  GUARD_DEPENDENCIES = {
      "gitea_push_verified_commit": ("git_fetch_ref",),
      "git_update_branch_by_merge": (),
      "git_push": (),
  }
  ```
  Do not widen it. `git_refresh_branch_to_head` gates nothing.
- `git_push` (`mcp_client_tools.py:2723`) has no CAS precondition. Fixing that is out of scope.
- No new MCP tool is registered by this plan. The observation rides the existing `tools_manifest`.

---

### Task 1: `surface_parity` module — observation store, coverage, gate

**Files:**
- Create: `examples/mcp_server/surface_parity.py`
- Test: `tests/test_surface_parity.py`

**Interfaces:**
- Consumes: `tool_error` from `tool_results` (signature: `tool_error(tool, code, message, *, hint, details, source, **extra_meta)`).
- Produces:
  - `GUARD_DEPENDENCIES: dict[str, tuple[str, ...]]`
  - `record_observation(reuse_key: str, toolset_hash: str, names: list[str], complete: bool) -> dict[str, Any]`
  - `load_bound_observation(reuse_key: str | None, toolset_hash: str | None) -> dict[str, Any] | None`
  - `build_client_observation(reuse_key, toolset_hash, names, complete) -> dict[str, Any]`
  - `build_guard_coverage(observation: dict | None) -> dict[str, Any]`
  - `build_server_surface(toolset_hash, names, schemas) -> dict[str, Any]`
  - `guard_violation(tool_name, reuse_key, toolset_hash) -> dict[str, Any] | None`
  - `clear_observations() -> None` (test seam)

- [ ] **Step 1: Write the failing test**

Create `tests/test_surface_parity.py`:

```python
"""Tests for external tool-surface observation and guard gating."""

from __future__ import annotations

import os
import sys

import pytest

_MCP_SERVER_DIR = os.path.join(os.path.dirname(__file__), "..", "examples", "mcp_server")
sys.path.insert(0, _MCP_SERVER_DIR)

from surface_parity import (  # noqa: E402
    GUARD_DEPENDENCIES,
    build_client_observation,
    build_guard_coverage,
    clear_observations,
    guard_violation,
    load_bound_observation,
    record_observation,
)

IDENTITY = "a" * 64
OTHER_IDENTITY = "b" * 64
HASH = "sha256:" + "c" * 64
STALE_HASH = "sha256:" + "d" * 64


@pytest.fixture(autouse=True)
def _clean_store():
    clear_observations()
    yield
    clear_observations()


class TestStore:
    def test_no_observation_loads_as_none(self) -> None:
        assert load_bound_observation(IDENTITY, HASH) is None

    def test_bound_observation_round_trips(self) -> None:
        record_observation(IDENTITY, HASH, ["git_fetch_ref", "git_push"], complete=True)
        loaded = load_bound_observation(IDENTITY, HASH)
        assert loaded is not None
        assert loaded["names"] == ["git_fetch_ref", "git_push"]
        assert loaded["complete"] is True

    def test_stale_toolset_hash_invalidates(self) -> None:
        record_observation(IDENTITY, HASH, ["git_fetch_ref"], complete=True)
        assert load_bound_observation(IDENTITY, STALE_HASH) is None

    def test_foreign_identity_does_not_load(self) -> None:
        record_observation(IDENTITY, HASH, ["git_fetch_ref"], complete=True)
        assert load_bound_observation(OTHER_IDENTITY, HASH) is None

    def test_latest_submission_replaces_previous(self) -> None:
        record_observation(IDENTITY, HASH, ["git_fetch_ref"], complete=True)
        record_observation(IDENTITY, HASH, ["git_push"], complete=True)
        loaded = load_bound_observation(IDENTITY, HASH)
        assert loaded is not None
        assert loaded["names"] == ["git_push"]


class TestGuardCoverage:
    def test_unknown_without_observation(self) -> None:
        coverage = build_guard_coverage(None)
        assert coverage["status"] == "unknown"

    def test_unknown_for_unlisted_dependency_when_incomplete(self) -> None:
        obs = build_client_observation(IDENTITY, HASH, ["git_push"], complete=False)
        coverage = build_guard_coverage(obs)
        entry = coverage["guards"]["gitea_push_verified_commit"]
        assert entry["status"] == "unknown"

    def test_absent_for_complete_report_missing_dependency(self) -> None:
        obs = build_client_observation(IDENTITY, HASH, ["git_push"], complete=True)
        coverage = build_guard_coverage(obs)
        entry = coverage["guards"]["gitea_push_verified_commit"]
        assert entry["status"] == "absent"
        assert entry["missing"] == ["git_fetch_ref"]

    def test_covered_for_complete_report_with_dependency(self) -> None:
        obs = build_client_observation(IDENTITY, HASH, ["git_fetch_ref"], complete=True)
        coverage = build_guard_coverage(obs)
        entry = coverage["guards"]["gitea_push_verified_commit"]
        assert entry["status"] == "covered"

    def test_tool_without_dependencies_is_never_uncovered(self) -> None:
        obs = build_client_observation(IDENTITY, HASH, [], complete=True)
        coverage = build_guard_coverage(obs)
        assert coverage["guards"]["git_push"]["status"] == "no_dependency"
        assert coverage["guards"]["git_update_branch_by_merge"]["status"] == "no_dependency"


class TestGate:
    def test_no_gate_without_observation(self) -> None:
        assert guard_violation("gitea_push_verified_commit", IDENTITY, HASH) is None

    def test_no_gate_without_identity(self) -> None:
        record_observation(IDENTITY, HASH, [], complete=True)
        assert guard_violation("gitea_push_verified_commit", None, HASH) is None

    def test_no_gate_for_incomplete_report(self) -> None:
        record_observation(IDENTITY, HASH, [], complete=False)
        assert guard_violation("gitea_push_verified_commit", IDENTITY, HASH) is None

    def test_gate_fires_on_complete_report_missing_dependency(self) -> None:
        record_observation(IDENTITY, HASH, ["git_push"], complete=True)
        err = guard_violation("gitea_push_verified_commit", IDENTITY, HASH)
        assert err is not None
        assert err["ok"] is False
        assert err["code"] == "REQUIRED_PREFLIGHT_UNAVAILABLE"
        assert "git_fetch_ref" in err["message"]

    def test_gate_silent_when_dependency_present(self) -> None:
        record_observation(IDENTITY, HASH, ["git_fetch_ref"], complete=True)
        assert guard_violation("gitea_push_verified_commit", IDENTITY, HASH) is None

    def test_gate_silent_for_tool_without_dependencies(self) -> None:
        record_observation(IDENTITY, HASH, [], complete=True)
        assert guard_violation("git_push", IDENTITY, HASH) is None
        assert guard_violation("git_update_branch_by_merge", IDENTITY, HASH) is None

    def test_gate_silent_after_redeploy(self) -> None:
        record_observation(IDENTITY, HASH, [], complete=True)
        assert guard_violation("gitea_push_verified_commit", IDENTITY, STALE_HASH) is None


class TestDependencyMap:
    def test_map_is_exactly_the_verified_set(self) -> None:
        assert GUARD_DEPENDENCIES == {
            "gitea_push_verified_commit": ("git_fetch_ref",),
            "git_update_branch_by_merge": (),
            "git_push": (),
        }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_surface_parity.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'surface_parity'`

- [ ] **Step 3: Write the implementation**

Create `examples/mcp_server/surface_parity.py`:

```python
"""External tool-surface observation and guard-dependency gating.

The Gateway cannot observe an external connector's tool catalog. ``tools/list``
is served in full, and whatever a client retains after import is invisible to the
server. This module therefore never *infers* external visibility from the local
manifest. It only:

1. stores an explicit, client-supplied observation of which tool names the caller
   can actually invoke;
2. binds that observation to the submitting identity and the current toolset hash,
   so it cannot be replayed by another caller or survive a redeploy;
3. derives guard coverage from a bound observation, and refuses a mutation only
   when a *complete* observation proves a specific guard dependency absent.

Absence of evidence never blocks. An unbound, stale, foreign-identity, or
incomplete observation leaves coverage ``unknown`` and every mutation proceeds.

Verified dependency map (spec section 1.5). Only
``gitea_push_verified_commit`` has a genuine cross-tool dependency: its required
``expected_base_sha`` is established from a ref pinned by ``git_fetch_ref``.
``git_update_branch_by_merge``'s ``expected_head`` is local state, readable via
``info(project)``. ``git_push`` carries no CAS precondition at all.
``git_refresh_branch_to_head`` gates nothing.
"""

from __future__ import annotations

from typing import Any

from tool_results import tool_error

GUARD_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "gitea_push_verified_commit": ("git_fetch_ref",),
    "git_update_branch_by_merge": (),
    "git_push": (),
}

_PARITY_SOURCE = "gateway"

# Process-local, never persisted: a stored observation would outlive the
# connector state it describes, which is exactly the staleness the
# toolset_hash binding exists to catch.
_OBSERVATIONS: dict[str, dict[str, Any]] = {}


def clear_observations() -> None:
    """Drop all stored observations. Test seam only."""
    _OBSERVATIONS.clear()


def record_observation(
    reuse_key: str,
    toolset_hash: str,
    names: list[str],
    complete: bool,
) -> dict[str, Any]:
    """Bind and store one observation for ``reuse_key``.

    The most recent submission for an identity replaces its predecessor.
    """
    cleaned = sorted({str(n) for n in names if str(n).strip()})
    record = {
        "names": cleaned,
        "complete": bool(complete),
        "toolset_hash": toolset_hash,
    }
    _OBSERVATIONS[reuse_key] = record
    return dict(record)


def load_bound_observation(
    reuse_key: str | None,
    toolset_hash: str | None,
) -> dict[str, Any] | None:
    """Return the observation only if it is bound to this identity and build.

    Either mismatch is independently disqualifying, so this returns ``None``
    unless both match.
    """
    if not reuse_key or not toolset_hash:
        return None
    record = _OBSERVATIONS.get(reuse_key)
    if record is None:
        return None
    if record.get("toolset_hash") != toolset_hash:
        return None
    return dict(record)


def build_server_surface(
    toolset_hash: str,
    names: list[str],
    schemas: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Describe this process's registered surface. Authoritative."""
    sorted_names = sorted({str(n) for n in names})
    return {
        "toolset_hash": toolset_hash,
        "registered_tool_count": len(sorted_names),
        "total_schema_bytes": sum((schemas or {}).values()),
        "tools": sorted_names,
    }


def build_client_observation(
    reuse_key: str | None,
    toolset_hash: str | None,
    names: list[str] | None,
    complete: bool = False,
) -> dict[str, Any]:
    """Wrap a client-supplied observation with its binding.

    ``status`` is ``"not_supplied"`` when nothing was submitted, and
    ``"supplied_unbound"`` when a report exists but cannot be tied to an
    identity and build — in which case it must never gate anything.
    """
    if names is None:
        return {
            "status": "not_supplied",
            "bound": False,
            "complete": False,
            "names": [],
            "toolset_hash": None,
            "guidance": (
                "Submit client_visible_tool_names (and set "
                "client_visible_tool_names_complete=true only if this is the "
                "complete externally visible set) to make external surface "
                "verifiable."
            ),
        }
    cleaned = sorted({str(n) for n in names if str(n).strip()})
    bound = bool(reuse_key) and bool(toolset_hash)
    return {
        "status": "supplied" if bound else "supplied_unbound",
        "bound": bound,
        "complete": bool(complete) if bound else False,
        "names": cleaned,
        "toolset_hash": toolset_hash if bound else None,
        "guidance": (
            "Completeness is unverified: names absent from an incomplete report "
            "are unknown, not absent."
        ),
    }


def build_guard_coverage(observation: dict[str, Any] | None) -> dict[str, Any]:
    """Derive per-mutation guard coverage from a bound observation.

    Never derived from an unbound or incomplete observation: coverage stays
    ``unknown`` so no gate can fire.
    """
    bound = bool(observation and observation.get("bound"))
    complete = bool(observation and observation.get("complete"))
    observed = set(observation.get("names", [])) if bound else set()

    guards: dict[str, dict[str, Any]] = {}
    for tool_name, deps in GUARD_DEPENDENCIES.items():
        if not deps:
            guards[tool_name] = {
                "dependencies": [],
                "status": "no_dependency",
                "missing": [],
            }
            continue
        if not bound:
            status = "unknown"
        elif not complete:
            status = "unknown"
        else:
            missing = sorted(d for d in deps if d not in observed)
            status = "absent" if missing else "covered"
            guards[tool_name] = {
                "dependencies": list(deps),
                "status": status,
                "missing": missing,
            }
            continue
        guards[tool_name] = {
            "dependencies": list(deps),
            "status": status,
            "missing": [],
        }

    overall = "unknown"
    if bound and complete:
        statuses = {g["status"] for g in guards.values()}
        if "absent" in statuses:
            overall = "absent"
        elif statuses <= {"covered", "no_dependency"}:
            overall = "covered"

    return {
        "status": overall,
        "observation_bound": bound,
        "observation_complete": complete,
        "guards": guards,
    }


def guard_violation(
    tool_name: str,
    reuse_key: str | None,
    toolset_hash: str | None,
) -> dict[str, Any] | None:
    """Return a typed error when a mutation's guard dependency is proven absent.

    Returns ``None`` — meaning "proceed" — in every other case, including no
    observation, an unbound one, an incomplete one, a stale ``toolset_hash``, an
    unknown tool, or a tool with no dependencies.
    """
    observation = load_bound_observation(reuse_key, toolset_hash)
    coverage = build_guard_coverage(observation)
    entry = coverage["guards"].get(tool_name)
    if entry is None or entry["status"] != "absent":
        return None
    missing = ", ".join(entry["missing"])
    return tool_error(
        tool=tool_name,
        code="REQUIRED_PREFLIGHT_UNAVAILABLE",
        message=(
            f"{tool_name} requires {missing}, which is absent from the complete "
            "externally visible tool surface reported for this caller."
        ),
        hint=(
            "Re-import the guard tool in the external connector, or submit an "
            "updated client_visible_tool_names observation."
        ),
        details={"missing_guard_tools": entry["missing"]},
        source=_PARITY_SOURCE,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_surface_parity.py -q`
Expected: PASS — every test in `TestStore`, `TestGuardCoverage`, `TestGate`, and `TestDependencyMap` green.

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/surface_parity.py tests/test_surface_parity.py
git commit -m "feat(mcp): add external tool-surface observation and guard gating"
```

---

### Task 2: Register the new error code

**Files:**
- Modify: `examples/mcp_server/tool_results.py:23` (`ERROR_CODES` set)
- Test: `tests/test_surface_parity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `EXTERNAL_RESOURCE_CATALOG_MISMATCH` present in `ERROR_CODES`, so `tool_error` stops downgrading it to `INTERNAL_ERROR`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_surface_parity.py`:

```python
class TestErrorCodeRegistration:
    def test_parity_codes_are_registered(self) -> None:
        from tool_results import ERROR_CODES

        assert "EXTERNAL_RESOURCE_CATALOG_MISMATCH" in ERROR_CODES
        assert "REQUIRED_PREFLIGHT_UNAVAILABLE" in ERROR_CODES
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_surface_parity.py::TestErrorCodeRegistration -q`
Expected: FAIL — `EXTERNAL_RESOURCE_CATALOG_MISMATCH` not in `ERROR_CODES`

- [ ] **Step 3: Register the code**

In `examples/mcp_server/tool_results.py`, inside the `ERROR_CODES` set, add `"EXTERNAL_RESOURCE_CATALOG_MISMATCH"` immediately after the existing `"GIT_WRITE_PREFLIGHT_UNAVAILABLE",` line.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_surface_parity.py tests/test_tool_results.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/tool_results.py tests/test_surface_parity.py
git commit -m "fix(mcp): register EXTERNAL_RESOURCE_CATALOG_MISMATCH error code"
```

---

### Task 3: Split the manifest contract into three explicit sections

**Files:**
- Modify: `examples/mcp_server/tools_manifest.py:36-56` (`_operator_surface_contract`)
- Modify: `examples/mcp_server/tools_manifest.py:59-71` (`build_manifest` signature) and `:210-224` (return dict)
- Test: `tests/test_tools_manifest.py`

**Interfaces:**
- Consumes: `build_server_surface`, `build_client_observation`, `build_guard_coverage` from Task 1.
- Produces: `build_manifest(..., toolset_hash=None, client_visible_tool_names=None, client_visible_tool_names_complete=False)`; `operator_surface_contract` gains `server_surface`, `client_observation`, `guard_coverage` while retaining every existing key.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools_manifest.py`:

```python
class TestOperatorSurfaceParitySections:
    def _manifest(self, **kwargs):
        tools = [FakeTool("git_fetch_ref"), FakeTool("git_push")]
        params = {
            "scope_enforcement": "enforce",
            "mode_override": "mcp_client_write",
            "toolset_hash": "sha256:" + "c" * 64,
            "reuse_key": "a" * 64,
        }
        params.update(kwargs)
        return build_manifest(tools, **params)

    def test_server_surface_is_authoritative(self) -> None:
        contract = self._manifest()["operator_surface_contract"]
        surface = contract["server_surface"]
        assert surface["toolset_hash"] == "sha256:" + "c" * 64
        assert surface["registered_tool_count"] == 2
        assert surface["tools"] == ["git_fetch_ref", "git_push"]

    def test_client_observation_absent_by_default(self) -> None:
        contract = self._manifest()["operator_surface_contract"]
        assert contract["client_observation"]["status"] == "not_supplied"
        assert contract["client_observation"]["bound"] is False

    def test_guard_coverage_unknown_without_observation(self) -> None:
        contract = self._manifest()["operator_surface_contract"]
        assert contract["guard_coverage"]["status"] == "unknown"
        assert (
            contract["guard_coverage"]["guards"]["gitea_push_verified_commit"]["status"]
            == "unknown"
        )

    def test_bound_complete_observation_reports_absent_dependency(self) -> None:
        contract = self._manifest(
            client_visible_tool_names=["git_push"],
            client_visible_tool_names_complete=True,
        )["operator_surface_contract"]
        coverage = contract["guard_coverage"]
        assert coverage["status"] == "absent"
        assert (
            coverage["guards"]["gitea_push_verified_commit"]["missing"]
            == ["git_fetch_ref"]
        )

    def test_unbound_observation_never_gates(self) -> None:
        contract = self._manifest(
            client_visible_tool_names=[],
            client_visible_tool_names_complete=True,
            toolset_hash=None,
        )["operator_surface_contract"]
        assert contract["client_observation"]["bound"] is False
        assert contract["guard_coverage"]["status"] == "unknown"

    def test_existing_contract_keys_are_preserved(self) -> None:
        contract = self._manifest()["operator_surface_contract"]
        assert contract["server_tool_manager_verified"] is True
        assert contract["server_tool_manager_surface"] == "mcp.tools/list"
        assert contract["external_resource_catalog"] == "api_tool.list_resources"
        assert contract["authoritative_for_external_schema_visibility"] is False
        assert contract["diagnostic_code"] == "EXTERNAL_RESOURCE_CATALOG_UNVERIFIED"

    def test_verified_flag_reflects_bound_observation_only(self) -> None:
        unverified = self._manifest()["operator_surface_contract"]
        assert unverified["external_resource_catalog_verified"] is False
        verified = self._manifest(
            client_visible_tool_names=["git_fetch_ref"],
            client_visible_tool_names_complete=True,
        )["operator_surface_contract"]
        assert verified["external_resource_catalog_verified"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_tools_manifest.py -q -k OperatorSurfaceParitySections`
Expected: FAIL — `TypeError: build_manifest() got an unexpected keyword argument 'toolset_hash'`

- [ ] **Step 3: Implement the contract split**

In `examples/mcp_server/tools_manifest.py`:

a) Extend the imports:

```python
from surface_parity import (
    build_client_observation,
    build_guard_coverage,
    build_server_surface,
)
```

b) Replace `_operator_surface_contract` (lines 36-56) with:

```python
def _operator_surface_contract(
    active_mode: str,
    scope_enforcement: str,
    server_names: list[str],
    toolset_hash: str | None,
    reuse_key: str | None,
    client_visible_tool_names: list[str] | None,
    client_visible_tool_names_complete: bool,
) -> dict[str, Any]:
    """Describe which discovery surface this manifest can and cannot verify.

    ``tools_manifest`` is built inside the Gateway MCP server from FastMCP's live
    tool manager plus repo-local mode/scope configuration.  It cannot observe an
    external connector or ChatGPT resource catalog that may cache or filter the
    same server's tools before the operator sees schemas.  Reporting that boundary
    explicitly prevents a server-local ``available=true`` entry from being
    mistaken for proof that another surface can invoke the tool.

    The contract is deliberately three separate sections:

    ``server_surface``
        Authoritative about this process and nothing else.
    ``client_observation``
        Client-supplied and unverified until bound to an identity and a
        ``toolset_hash``.
    ``guard_coverage``
        Derived by the Gateway from a bound observation only.  A caller may
        never assert its own coverage.
    """
    observation = build_client_observation(
        reuse_key,
        toolset_hash,
        client_visible_tool_names,
        client_visible_tool_names_complete,
    )
    coverage = build_guard_coverage(
        observation if observation["bound"] else None
    )
    verified = coverage["status"] == "covered"

    return {
        "server_tool_manager_verified": True,
        "server_tool_manager_surface": "mcp.tools/list",
        "active_mode": active_mode,
        "scope_enforcement": scope_enforcement,
        "server_surface": build_server_surface(
            toolset_hash or "", server_names
        ),
        "client_observation": observation,
        "guard_coverage": coverage,
        "external_resource_catalog": "api_tool.list_resources",
        "external_resource_catalog_verified": verified,
        "authoritative_for_external_schema_visibility": False,
        "diagnostic_code": (
            "EXTERNAL_RESOURCE_CATALOG_MISMATCH"
            if observation["status"] == "supplied"
            else "EXTERNAL_RESOURCE_CATALOG_UNVERIFIED"
        ),
        "operator_guidance": "Use server_surface as the server-local MCP tool-manager view. client_observation is only as trustworthy as the caller that submitted it, and is unbound — therefore never gating — until it carries both an identity and a toolset_hash. Before assuming ChatGPT/api_tool invocation is possible, submit the externally visible tool names here; a guard dependency reported absent under guard_coverage is a proven external catalog mismatch, not proof that the MCP server failed to register the tool.",
    }
```

c) Extend the `build_manifest` signature with three keyword-only parameters after `unavailable_tool_reasons`:

```python
    toolset_hash: str | None = None,
    reuse_key: str | None = None,
    client_visible_tool_names: list[str] | None = None,
    client_visible_tool_names_complete: bool = False,
```

d) Replace the `operator_surface_contract` entry in the returned dict:

```python
        "operator_surface_contract": _operator_surface_contract(
            active_mode,
            scope_enforcement,
            sorted(registered_names),
            toolset_hash,
            reuse_key,
            client_visible_tool_names,
            client_visible_tool_names_complete,
        ),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_tools_manifest.py -q`
Expected: PASS — including the pre-existing `test_operator_surface_contract_marks_external_catalog_unverified`, which still holds because an unobserved manifest reports `verified: False`.

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/tools_manifest.py tests/test_tools_manifest.py
git commit -m "feat(mcp): split operator_surface_contract into server/client/guard sections"
```

---

### Task 4: Accept and record the observation on `tools_manifest`

**Files:**
- Modify: `examples/mcp_server/server.py:411-445` (`gateway_tools_manifest`)
- Test: `tests/test_surface_parity.py`

**Interfaces:**
- Consumes: `build_manifest` (Task 3), `compute_toolset_hash` (already exported at `server.py:232`), `_current_auth_reuse_key` (`server.py:171`), `record_observation` (Task 1).
- Produces: `tools_manifest(client_visible_tool_names=None, client_visible_tool_names_complete=False)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_surface_parity.py`:

```python
class TestManifestRecordsObservation:
    def test_server_records_submitted_observation(self) -> None:
        server = pytest.importorskip("server")
        from surface_parity import load_bound_observation

        server.gateway_tools_manifest(
            client_visible_tool_names=["git_fetch_ref", "git_push"],
            client_visible_tool_names_complete=True,
        )
        loaded = load_bound_observation(
            server._current_auth_reuse_key(),
            server.compute_toolset_hash(server.mcp),
        )
        assert loaded is not None
        assert loaded["complete"] is True
        assert "git_fetch_ref" in loaded["names"]

    def test_server_manifest_defaults_to_no_observation(self) -> None:
        server = pytest.importorskip("server")
        result = server.gateway_tools_manifest()
        assert result["ok"] is True
        contract = result["result"]["operator_surface_contract"]
        assert contract["client_observation"]["status"] == "not_supplied"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_surface_parity.py -q -k ManifestRecordsObservation`
Expected: FAIL — `TypeError: gateway_tools_manifest() got an unexpected keyword argument 'client_visible_tool_names'`

- [ ] **Step 3: Implement the wiring**

In `examples/mcp_server/server.py`, replace `gateway_tools_manifest` (lines 411-445) with:

```python
@register_tool("tools_manifest")
def gateway_tools_manifest(
    scope: str | None = None,
    mode: str | None = None,
    name_prefix: str | None = None,
    include_descriptions: bool = True,
    offset: int = 0,
    limit: int | None = None,
    client_visible_tool_names: list[str] | None = None,
    client_visible_tool_names_complete: bool = False,
) -> dict[str, Any]:
    """Return a read-only manifest of registered tools, modes, scopes, and access profiles.
    No secrets, no env dumps, no network calls, no tool execution.

    Optional filters (scope/mode/name_prefix) and include_descriptions/
    offset/limit keep the response small -- the unfiltered manifest lists
    every registered tool's full description, which is expensive context
    for an agent that just needs, say, the docker_* tool names. Each
    entry also reports "available" (and "unavailable_reason" when false)
    -- distinct from "enabled": a tool can be registered/enabled for this
    mode yet still not actually work in this deployment (missing docker/
    npx binary, Postgres not configured).

    ``client_visible_tool_names`` lets an external caller report which tool
    names it can actually invoke, which the server cannot observe on its own.
    The report is bound to the submitting identity and the current toolset
    hash, and is therefore only usable as a safety input while both still
    match. Set ``client_visible_tool_names_complete`` only when the submitted
    list is the complete externally visible set: names missing from an
    incomplete report are treated as unknown, never as absent, so an honest
    partial report can never lock a caller out of a mutation.
    """
    toolset_hash = compute_toolset_hash(mcp)
    reuse_key = _current_auth_reuse_key()
    if client_visible_tool_names is not None and reuse_key and toolset_hash:
        record_observation(
            reuse_key,
            toolset_hash,
            list(client_visible_tool_names),
            bool(client_visible_tool_names_complete),
        )
    return _run_gateway(
        tool="tools_manifest",
        fn=lambda: _build_manifest(
            registered_tools=mcp._tool_manager.list_tools(),
            scope_enforcement=_scope_enforcement,
            scope=scope,
            mode=mode,
            name_prefix=name_prefix,
            include_descriptions=include_descriptions,
            offset=offset,
            limit=limit,
            unavailable_tool_reasons=_unavailable_tool_reasons(),
            toolset_hash=toolset_hash,
            reuse_key=reuse_key,
            client_visible_tool_names=client_visible_tool_names,
            client_visible_tool_names_complete=client_visible_tool_names_complete,
        ),
    )
```

Add the import next to the existing manifest import at `server.py:332`:

```python
from surface_parity import record_observation  # noqa: E402
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_surface_parity.py tests/test_tools_manifest.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/server.py tests/test_surface_parity.py
git commit -m "feat(mcp): accept and bind external tool-surface observations"
```

---

### Task 5: Gate `gitea_push_verified_commit`

**Files:**
- Modify: `examples/mcp_server/mcp_infra/adapters/remote.py:2536` (`gitea_push_verified_commit`)
- Test: `tests/test_surface_parity.py`

**Interfaces:**
- Consumes: `guard_violation` (Task 1), `_caller_fingerprint` (`remote.py:93`, already resolves `_current_auth_reuse_key` via `server_attr`), `server_attr` (`mcp_infra/_server_ref.py:28`).
- Produces: module-level `_current_toolset_hash()` in `remote.py`, returning `str | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_surface_parity.py`:

```python
class TestRemoteAdapterGate:
    def test_current_toolset_hash_helper_exists(self) -> None:
        from examples.mcp_server.mcp_infra.adapters import remote

        assert hasattr(remote, "_current_toolset_hash")

    def test_hash_helper_degrades_instead_of_raising(self, monkeypatch) -> None:
        from examples.mcp_server.mcp_infra.adapters import remote

        def _boom(name):
            raise RuntimeError("server module unavailable")

        monkeypatch.setattr(remote, "server_attr", _boom)
        # An unresolvable hash must return None, not raise: it leaves the gate
        # silent rather than blocking a legitimate mutation.
        assert remote._current_toolset_hash() is None

    def test_hash_helper_degrades_when_hasher_raises(self, monkeypatch) -> None:
        from examples.mcp_server.mcp_infra.adapters import remote

        monkeypatch.setattr(
            remote,
            "server_attr",
            lambda name: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert remote._current_toolset_hash() is None

    def test_gate_is_consulted_by_push(self) -> None:
        import inspect

        from examples.mcp_server.mcp_infra.adapters import remote

        src = inspect.getsource(remote.gitea_push_verified_commit)
        assert "guard_violation" in src
        # The gate must run before any GITEA_TOKEN work or network call.
        assert src.index("guard_violation") < src.index("GITEA_TOKEN")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_surface_parity.py -q -k RemoteAdapterGate`
Expected: FAIL — `_current_toolset_hash` missing, and `guard_violation` absent from the function body

- [ ] **Step 3: Implement the gate**

In `examples/mcp_server/mcp_infra/adapters/remote.py`:

a) Add the import near the other `tool_results` import (line 24):

```python
from surface_parity import guard_violation
```

b) Add the hash helper directly after `_caller_fingerprint` (after line 105):

```python
def _current_toolset_hash() -> str | None:
    """Return the live MCP toolset hash, or None when it cannot be resolved.

    Used to bind an external surface observation to the exact catalog it was
    reported against, so a redeploy invalidates it.  Degrades to None rather
    than raising: an unresolvable hash must leave the guard gate silent, never
    block a legitimate mutation.
    """
    try:
        mcp_instance = server_attr("mcp")
        hasher = server_attr("compute_toolset_hash")
    except Exception:
        return None
    try:
        return str(hasher(mcp_instance))
    except Exception:
        return None
```

c) Insert the gate as the first statement of `gitea_push_verified_commit`, before the `GITEA_TOKEN` read at line 2547:

```python
    parity_violation = guard_violation(
        "gitea_push_verified_commit",
        _caller_fingerprint(),
        _current_toolset_hash(),
    )
    if parity_violation is not None:
        return parity_violation
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_surface_parity.py -q`
Expected: PASS

- [ ] **Step 5: Run the remote adapter's existing tests**

Run: `pytest tests/ -q -k "gitea_push_verified or remote or gitea"`
Expected: PASS — the gate is silent without a bound observation, so existing behaviour is unchanged.

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_server/mcp_infra/adapters/remote.py tests/test_surface_parity.py
git commit -m "feat(mcp): gate verified-commit push on proven-absent guard dependency"
```

---

### Task 6: Lint, typecheck, and update TODO

**Files:**
- Modify: `TODO.md`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: no new interfaces.

- [ ] **Step 1: Run Ruff**

Run: `ruff format examples/mcp_server/surface_parity.py examples/mcp_server/tools_manifest.py examples/mcp_server/server.py examples/mcp_server/tool_results.py examples/mcp_server/mcp_infra/adapters/remote.py tests/test_surface_parity.py tests/test_tools_manifest.py && ruff check examples/mcp_server tests`
Expected: no errors. If `ruff format` rewrites anything, re-run `ruff check`.

- [ ] **Step 2: Run mypy**

Run: `mypy examples/mcp_server/surface_parity.py examples/mcp_server/tools_manifest.py`
Expected: no errors.

- [ ] **Step 3: Run the full test suite**

Run: `pytest -q`
Expected: all pass. Any pre-existing failure unrelated to this plan must be recorded, not silently fixed.

- [ ] **Step 4: Update TODO.md**

Find the P1 parity entry and change its text so it states the remaining work honestly — the ChatGPT-visible parity P1 **stays open** until §2.6 of the spec has been measured on the real connector:

```markdown
- [ ] P1: ChatGPT-visible tool parity — Gateway-side measurement shipped
      (`tools_manifest` reports `server_surface` / `client_observation` /
      `guard_coverage`; `gitea_push_verified_commit` fails closed on a
      proven-absent `git_fetch_ref`). **Still open:** submit a real
      `client_visible_tool_names` observation from the external connector and
      record whether the missing guard tools indicate a catalog count cap, a
      name allowlist, or a connector cache. Only then decide on a curated
      catalog (A2) or caching (A3).
```

- [ ] **Step 5: Commit**

```bash
git add TODO.md
git commit -m "docs: record remaining external parity measurement in TODO"
```

---

## Post-merge: PR-C live verification

Not code — executed against a real Gateway deployment once this plan is merged
and deployed. Follow spec §7 verbatim. Do not modify or merge `quart-core #227`
itself.

## Self-Review

**1. Spec coverage.** Spec §2.1 three sections → Task 3. §2.2 observation
channel + completeness default → Tasks 1, 3, 4. §2.3 binding and storage →
Tasks 1, 4. §2.4 diagnostics → Tasks 1, 2. §2.5 gate and verified dependency map
→ Tasks 1, 5. §2.6 measurement → Task 6 TODO plus the post-merge protocol
(no code, correctly). §1.5 dependency analysis → encoded in Task 1's
`GUARD_DEPENDENCIES` and asserted by `test_map_is_exactly_the_verified_set`.

**2. Placeholder scan.** No `TBD`, no "add appropriate handling", no "similar to
Task N". Every code step carries literal code. One deliberate instruction is
non-mechanical: locating `"EXTERNAL_RESOURCE_CATALOG_MISMATCH"` immediately after
`"GIT_WRITE_PREFLIGHT_UNAVAILABLE,"` in the `ERROR_CODES` set — the surrounding
code is quoted in Task 2's test so the anchor is unambiguous.

**3. Type consistency.** `guard_violation(tool_name, reuse_key, toolset_hash)`
is defined once in Task 1 and called with that exact positional order in Task 5.
`record_observation(reuse_key, toolset_hash, names, complete)` is called with
keywords in Task 4 and positionally in Task 1's own test — consistent.
`build_client_observation` returns a dict whose `bound` key Task 3 reads and Task
1's tests assert; `build_guard_coverage` returns `guards` keyed by tool name in
both. `_current_toolset_hash` is introduced in Task 5 and referenced only there.