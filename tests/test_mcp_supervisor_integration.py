"""MCP adapter tests for admin-only supervisor integration tools."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

from examples.mcp_server.mcp_infra.adapters import supervisor
from examples.mcp_server.supervisor_integration import RecoveryResult


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _Registry:
    def __init__(self, roots: dict[str, Path]):
        self.roots = roots

    def project_info(self, project: str):
        root = self.roots.get(project)
        if root is None:
            raise ValueError("unknown project")
        return {"project_id": project, "root": str(root)}


@pytest.fixture
def immediate_run_tool(monkeypatch):
    def _run_tool(*, tool, title, fn, success_text):
        del tool, title, success_text
        return fn()

    monkeypatch.setattr(supervisor, "run_tool", _run_tool)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(supervisor, "_get_workspace_registry", lambda: _Registry({"demo": root}))
    journal_base = tmp_path / "journals"
    monkeypatch.setenv("MCP_SUPERVISOR_JOURNAL_ROOT", str(journal_base))
    return root, journal_base


def test_public_signatures_do_not_expose_roots():
    integrate = inspect.signature(supervisor.supervisor_integrate_file)
    assert list(integrate.parameters) == [
        "project",
        "relative_path",
        "expected_sha256",
        "new_content",
    ]
    recover = inspect.signature(supervisor.supervisor_recover_integrations)
    assert list(recover.parameters) == ["project"]
    assert "journal_root" not in integrate.parameters
    assert "project_root" not in integrate.parameters


def test_journal_namespace_is_server_controlled_and_project_specific(tmp_path, monkeypatch):
    base = tmp_path / "journal-base"
    monkeypatch.setenv("MCP_SUPERVISOR_JOURNAL_ROOT", str(base))
    p1 = tmp_path / "one"
    p2 = tmp_path / "two"
    p1.mkdir()
    p2.mkdir()

    j1 = supervisor._journal_root_for_project("one", p1)
    j2 = supervisor._journal_root_for_project("two", p2)

    assert j1.parent == base.resolve()
    assert j2.parent == base.resolve()
    assert j1 != j2
    assert len(j1.name) == 64
    int(j1.name, 16)
    assert p1.resolve() not in j1.parents
    assert p2.resolve() not in j2.parents


def test_relative_journal_configuration_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SUPERVISOR_JOURNAL_ROOT", "relative/journals")
    project_root = tmp_path / "project"
    project_root.mkdir()
    with pytest.raises(ValueError, match="absolute"):
        supervisor._journal_root_for_project("demo", project_root)


def test_journal_configuration_inside_checkout_fails_closed(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv(
        "MCP_SUPERVISOR_JOURNAL_ROOT",
        str(project_root / ".private-journals"),
    )
    with pytest.raises(ValueError, match="outside"):
        supervisor._journal_root_for_project("demo", project_root)


def test_integrate_changes_existing_file_without_leaking_host_path(
    project, immediate_run_tool
):
    root, journal_base = project
    target = root / "config.txt"
    original = b"old\n"
    target.write_bytes(original)

    result = supervisor.supervisor_integrate_file(
        "demo",
        "config.txt",
        _sha(original),
        "new\n",
    )

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "new\n"
    assert result["result"] == {
        "project": "demo",
        "path": "config.txt",
        "original_hash": _sha(original),
        "new_hash": _sha(b"new\n"),
    }
    assert str(root) not in repr(result)
    assert str(journal_base) not in repr(result)


def test_hash_mismatch_is_canonical_and_path_safe(project, immediate_run_tool):
    root, journal_base = project
    target = root / "config.txt"
    target.write_text("old\n", encoding="utf-8")

    result = supervisor.supervisor_integrate_file(
        "demo",
        "config.txt",
        _sha(b"different"),
        "new\n",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "CHECK_FAILED"
    assert result["error"]["retryable"] is False
    assert str(root) not in repr(result)
    assert str(journal_base) not in repr(result)
    assert target.read_text(encoding="utf-8") == "old\n"


def test_unknown_project_fails_closed(monkeypatch, immediate_run_tool):
    monkeypatch.setattr(supervisor, "_get_workspace_registry", lambda: _Registry({}))

    result = supervisor.supervisor_integrate_file(
        "missing",
        "config.txt",
        _sha(b"old"),
        "new",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "PROJECT_NOT_FOUND"
    assert result["error"]["retryable"] is False


def test_non_text_content_is_rejected(project, immediate_run_tool):
    result = supervisor.supervisor_integrate_file(
        "demo",
        "config.txt",
        _sha(b"old"),
        b"bytes are not MCP text",  # type: ignore[arg-type]
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


def test_recovery_serializes_without_raw_error_or_paths(
    project, immediate_run_tool, monkeypatch
):
    root, journal_base = project
    monkeypatch.setattr(
        supervisor,
        "recover_pending",
        lambda project_root, journal_root: [
            RecoveryResult(
                relative_path="a.txt",
                status="restored",
                journal_retained=False,
            ),
            RecoveryResult(
                relative_path="b.txt",
                status="error",
                journal_retained=True,
                error=f"unsafe target {root}/b.txt journal {journal_root}",
            ),
        ],
    )

    result = supervisor.supervisor_recover_integrations("demo")

    assert result["ok"] is True
    assert result["result"]["project"] == "demo"
    assert result["result"]["count"] == 2
    assert result["result"]["recoveries"] == [
        {
            "path": "a.txt",
            "status": "restored",
            "journal_retained": False,
        },
        {
            "path": "b.txt",
            "status": "error",
            "journal_retained": True,
            "error": "Manual intervention is required for this recovery entry.",
        },
    ]
    assert str(root) not in repr(result)
    assert str(journal_base) not in repr(result)


def test_recovery_unknown_project_fails_closed(monkeypatch, immediate_run_tool):
    monkeypatch.setattr(supervisor, "_get_workspace_registry", lambda: _Registry({}))
    result = supervisor.supervisor_recover_integrations("missing")
    assert result["ok"] is False
    assert result["error"]["code"] == "PROJECT_NOT_FOUND"


def test_register_all_registers_exactly_two_tools(monkeypatch):
    registered: list[str] = []

    def _register(name):
        def _decorator(fn):
            registered.append(name)
            return fn

        return _decorator

    monkeypatch.setattr(supervisor, "register_tool", _register)
    monkeypatch.setattr(supervisor, "instrumented", lambda name: (lambda fn: fn))

    supervisor.register_all()

    assert registered == [
        "supervisor_integrate_file",
        "supervisor_recover_integrations",
    ]
