"""Backend contract normalization tests.

Verifies that ``build_task_json`` produces a correct ``allowed_backends``
allowlist at creation time, and that ``project_run_agent`` consumes it
correctly (including legacy-task backward compatibility).

Contract:
- ``opencode`` — concrete backend
- ``auto`` — selection mode, not a concrete backend
- ``allowed_backends`` — always an explicit non-empty set of permitted concrete backends
- Unknown concrete backend names must fail at creation
- Legacy task without ``allowed_backends`` but with ``agent="opencode"`` → fallback ``["opencode"]``
- Legacy ``agent="auto"`` without allowlist → fail closed
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from examples.mcp_server.agent_tasks import (
    KNOWN_CONCRETE_BACKENDS,
    build_task_json,
)
from examples.mcp_server.agent_tools import project_run_agent

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_task_json(
    *,
    task_id: str = "a12345678901",
    agent: str = "auto",
    allowed_backends: list[str] | None = None,
) -> dict[str, Any]:
    """Build task.json content and parse it back to a dict."""
    kwargs: dict[str, Any] = {"task_id": task_id, "agent": agent}
    if allowed_backends is not None:
        kwargs["allowed_backends"] = allowed_backends
    return json.loads(build_task_json(**kwargs))


def _run_cmd_factory(task_json: dict[str, Any], plan: str = "# Plan"):
    """Create a run_cmd that returns task.json and current-plan.md content."""

    def _run_cmd(project: str, command: str) -> dict[str, Any]:
        if "task.json" in command:
            return {"stdout": json.dumps(task_json), "stderr": "", "exit_code": 0}
        if "current-plan.md" in command:
            return {"stdout": plan, "stderr": "", "exit_code": 0}
        return {"stdout": "", "stderr": "", "exit_code": 0}

    return _run_cmd


def _run_script_factory() -> tuple[callable, list]:
    """Create a run_script that records calls and returns success."""
    calls: list[tuple[str, str]] = []

    def _run_script(project: str, script: str) -> dict[str, Any]:
        calls.append((project, script))
        return {"stdout": "ok", "stderr": "", "exit_code": 0}

    return _run_script, calls


# ── Known backends sanity check ──────────────────────────────────────────────


class TestKnownBackends:
    def test_opencode_is_known(self):
        assert "opencode" in KNOWN_CONCRETE_BACKENDS

    def test_auto_is_not_known(self):
        assert "auto" not in KNOWN_CONCRETE_BACKENDS


# ── 1. build_task_json(agent="auto") → ["opencode"] ─────────────────────────


class TestBuildTaskAutoDefaultsToOpencode:
    def test_agent_auto_no_allowlist(self):
        data = _make_task_json(agent="auto")
        assert data["allowed_backends"] == ["opencode"]

    def test_agent_auto_no_allowlist_full_roundtrip(self):
        raw = build_task_json(task_id="a12345678901", agent="auto")
        data = json.loads(raw)
        assert data["allowed_backends"] == ["opencode"]
        assert data["agent"] == "auto"


# ── 2. build_task_json(agent="opencode") → ["opencode"] ─────────────────────


class TestBuildTaskOpencodeDefaultsToOpencode:
    def test_agent_opencode_no_allowlist(self):
        data = _make_task_json(agent="opencode")
        assert data["allowed_backends"] == ["opencode"]

    def test_agent_opencode_no_allowlist_full_roundtrip(self):
        raw = build_task_json(task_id="a12345678901", agent="opencode")
        data = json.loads(raw)
        assert data["allowed_backends"] == ["opencode"]


# ── 3. build_task_json(agent="auto", allowed_backends=["opencode"]) → ["opencode"] ─


class TestBuildTaskAutoExplicitAllowlist:
    def test_explicit_single(self):
        data = _make_task_json(agent="auto", allowed_backends=["opencode"])
        assert data["allowed_backends"] == ["opencode"]

    def test_explicit_preserves_order(self):
        data = _make_task_json(agent="opencode", allowed_backends=["opencode"])
        assert data["allowed_backends"] == ["opencode"]


# ── 4. build_task_json(agent="auto", allowed_backends=["opencode", "unknown"]) → REJECTED


class TestBuildTaskRejectsUnknownBackend:
    def test_unknown_in_allowlist(self):
        with pytest.raises(ValueError, match="unknown backend"):
            build_task_json(
                task_id="a12345678901",
                agent="auto",
                allowed_backends=["opencode", "unknown"],
            )

    def test_only_unknown_in_allowlist(self):
        with pytest.raises(ValueError, match="unknown backend"):
            build_task_json(
                task_id="a12345678901",
                agent="auto",
                allowed_backends=["fictional-agent"],
            )

    def test_unknown_agent_name(self):
        with pytest.raises(ValueError, match="not a known concrete backend"):
            build_task_json(
                task_id="a12345678901",
                agent="nonexistent",
            )


# ── 5. E2E: producer task.json → project_run_agent → opencode selected (router disabled) ──


class TestE2ERouterDisabled:
    @pytest.fixture(autouse=True)
    def _patch_project_root(self, monkeypatch):
        """Avoid real SSH/workspace registry calls."""
        monkeypatch.setattr(
            "examples.mcp_server.agent_tools._resolve_project_root",
            lambda _project: None,
        )
        monkeypatch.setattr(
            "examples.mcp_server.agent_tools.managed_workspace_path",
            lambda _project, _task_id: None,
        )

    def test_auto_produces_opencode_task(self):
        task_json = _make_task_json(agent="auto")
        run_cmd = _run_cmd_factory(task_json)
        run_script, calls = _run_script_factory()

        result = project_run_agent(
            run_cmd,
            project="test-proj",
            task_id="a12345678901",
            router=None,
            run_script=run_script,
        )

        assert result["status"] == "needs-review"
        assert result["exit_code"] == 0
        assert len(calls) == 1

    def test_opencode_produces_opencode_task(self):
        task_json = _make_task_json(agent="opencode")
        run_cmd = _run_cmd_factory(task_json)
        run_script, calls = _run_script_factory()

        result = project_run_agent(
            run_cmd,
            project="test-proj",
            task_id="a12345678901",
            router=None,
            run_script=run_script,
        )

        assert result["status"] == "needs-review"
        assert result["exit_code"] == 0
        assert len(calls) == 1

    def test_explicit_allowlist_roundtrips(self):
        task_json = _make_task_json(agent="auto", allowed_backends=["opencode"])
        run_cmd = _run_cmd_factory(task_json)
        run_script, calls = _run_script_factory()

        result = project_run_agent(
            run_cmd,
            project="test-proj",
            task_id="a12345678901",
            router=None,
            run_script=run_script,
        )

        assert result["status"] == "needs-review"
        assert len(calls) == 1


# ── 6. E2E: producer task.json → project_run_agent → opencode selected (router enabled) ──


class TestE2ERouterEnabled:
    @pytest.fixture(autouse=True)
    def _patch_project_root(self, monkeypatch):
        monkeypatch.setattr(
            "examples.mcp_server.agent_tools._resolve_project_root",
            lambda _project: None,
        )
        monkeypatch.setattr(
            "examples.mcp_server.agent_tools.managed_workspace_path",
            lambda _project, _task_id: None,
        )

    def test_auto_with_router(self):
        from examples.mcp_server.agent_backend_router import AgentBackendRouter, BackendEntry

        router = AgentBackendRouter(
            backends={"opencode": BackendEntry(name="opencode", priority=0)},
            enabled=True,
        )
        task_json = _make_task_json(agent="auto")
        run_cmd = _run_cmd_factory(task_json)
        run_script, calls = _run_script_factory()

        result = project_run_agent(
            run_cmd,
            project="test-proj",
            task_id="a12345678901",
            router=router,
            run_script=run_script,
        )

        assert result["status"] == "needs-review"
        assert result["exit_code"] == 0
        assert len(calls) == 1


# ── 7. Empty explicit allowlist cannot become wildcard ────────────────────────


class TestEmptyAllowlistNotWildcard:
    def test_explicit_empty_list_fails_at_creation(self):
        with pytest.raises(ValueError, match="allowed_backends is empty"):
            build_task_json(
                task_id="a12345678901",
                agent="auto",
                allowed_backends=[],
            )

    def test_legacy_empty_allowed_backends_fails_execution(self):
        """Legacy task.json with allowed_backends=[] must not be treated as wildcard."""
        task_json = {
            "task_id": "a12345678901",
            "agent": "auto",
            "allowed_backends": [],
            "worktree_path": "",
            "base_ref": "",
        }
        run_cmd = _run_cmd_factory(task_json)

        result = project_run_agent(
            run_cmd,
            project="test-proj",
            task_id="a12345678901",
            router=None,
        )

        assert result["status"] == "error"
        assert "missing allowed_backends" in result["error"]


# ── 8. Unknown backend rejected during task creation ─────────────────────────


class TestUnknownBackendRejectedAtCreation:
    def test_rejects_unknown_in_allowlist(self):
        with pytest.raises(ValueError, match="unknown backend"):
            build_task_json(
                task_id="a12345678901",
                agent="auto",
                allowed_backends=["opencode", "fictional"],
            )

    def test_rejects_only_unknown(self):
        with pytest.raises(ValueError, match="unknown backend"):
            build_task_json(
                task_id="a12345678901",
                agent="auto",
                allowed_backends=["mimo"],
            )

    def test_rejects_unknown_agent(self):
        with pytest.raises(ValueError, match="not a known concrete backend"):
            build_task_json(
                task_id="a12345678901",
                agent="mimo",
            )


# ── 9. Legacy opencode/no-allowed_backends still executes (backward compat) ──


class TestLegacyOpencodeBackwardCompat:
    @pytest.fixture(autouse=True)
    def _patch_project_root(self, monkeypatch):
        monkeypatch.setattr(
            "examples.mcp_server.agent_tools._resolve_project_root",
            lambda _project: None,
        )
        monkeypatch.setattr(
            "examples.mcp_server.agent_tools.managed_workspace_path",
            lambda _project, _task_id: None,
        )

    def test_legacy_opencode_no_backends_runs(self):
        """Legacy task.json with agent='opencode' and no allowed_backends
        should fall back to ['opencode'] at execution time."""
        task_json = {
            "task_id": "a12345678901",
            "agent": "opencode",
            "allowed_files": [],
            "forbidden_files": [],
            "required_checks": [],
            "worktree_path": "",
            "base_ref": "",
        }
        run_cmd = _run_cmd_factory(task_json)
        run_script, calls = _run_script_factory()

        result = project_run_agent(
            run_cmd,
            project="test-proj",
            task_id="a12345678901",
            router=None,
            run_script=run_script,
        )

        assert result["status"] == "needs-review"
        assert result["exit_code"] == 0
        assert len(calls) == 1


# ── 10. Legacy auto/no-allowed_backends fails closed ─────────────────────────


class TestLegacyAutoFailsClosed:
    def test_legacy_auto_no_backends_fails(self):
        """Legacy task.json with agent='auto' and no allowed_backends
        must fail closed — cannot reconstruct original authorization."""
        task_json = {
            "task_id": "a12345678901",
            "agent": "auto",
            "allowed_files": [],
            "forbidden_files": [],
            "required_checks": [],
            "worktree_path": "",
            "base_ref": "",
        }
        run_cmd = _run_cmd_factory(task_json)

        result = project_run_agent(
            run_cmd,
            project="test-proj",
            task_id="a12345678901",
            router=None,
        )

        assert result["status"] == "error"
        assert "missing allowed_backends" in result["error"]

    def test_legacy_auto_with_backends_still_works(self):
        """Legacy task.json with agent='auto' AND allowed_backends should work."""
        task_json = {
            "task_id": "a12345678901",
            "agent": "auto",
            "allowed_backends": ["opencode"],
            "allowed_files": [],
            "forbidden_files": [],
            "required_checks": [],
            "worktree_path": "",
            "base_ref": "",
        }
        run_cmd = _run_cmd_factory(task_json)

        result = project_run_agent(
            run_cmd,
            project="test-proj",
            task_id="a12345678901",
            router=None,
        )

        # Contract gate passes; mock run_cmd returns exit_code=0
        assert result["status"] == "needs-review"
        assert "missing allowed_backends" not in result.get("error", "")
