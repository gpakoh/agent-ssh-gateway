from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.workspace.policy import ALL_SCOPES
from app.workspace.registry import WorkspaceRegistry
from app.workspace.todo_backlog import TodoBacklogError, upsert_todo_backlog_entry

PROJECT_ID = "fixture-project"


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[WorkspaceRegistry, Path]:
    monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    root = tmp_path / "registry-root"
    project = root / PROJECT_ID
    project.mkdir(parents=True)
    (project / "TODO.md").write_text(
        "# Fixture TODO\n\n## Existing section — 2026-09-06\n\n"
        "1. ⬜ **Existing gateway failure.**\n"
        "   <!-- gateway-todo-key: existing-gateway-failure -->\n"
        "   **Severity:** P2\n",
        encoding="utf-8",
    )
    registry_yaml = tmp_path / "projects.yaml"
    registry_yaml.write_text(
        "\n".join(
            [
                "version: 1",
                f"registry_root: {root}",
                "projects:",
                f"  {PROJECT_ID}:",
                f"    root: {PROJECT_ID}",
                "    type: service",
                "    description: Fixture project",
                "    tags:",
                "      - fixture",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return WorkspaceRegistry.load(registry_yaml, granted_scopes=ALL_SCOPES), project


def _entry_kwargs(**overrides: str):
    base: dict[str, str] = {
        "title": "Gateway TODO writer loses findings",
        "severity": "P2",
        "observed_behavior": "Operators paste backlog findings into chat instead of durable TODO.md.",
        "reproduction_steps": "Observe a delivery session with a new Gateway defect and no safe writer tool.",
        "expected_behavior": "One safe tool call records a structured TODO.md entry.",
        "impact": "Findings can be lost between conversations or require Docker-admin fallbacks.",
        "acceptance_criteria": "Duplicate titles are refused and TODO-only delivery remains possible.",
        "related_evidence": "manual operator request 2026-09-07",
        "entry_date": "2026-09-07",
    }
    base.update(overrides)
    return base


def test_creates_dated_section_and_structured_entry(registry):
    workspace_registry, project = registry

    result = upsert_todo_backlog_entry(
        project_id=PROJECT_ID,
        registry=workspace_registry,
        safe=True,
        **_entry_kwargs(),
    )

    content = (project / "TODO.md").read_text(encoding="utf-8")
    assert result["action"] == "created"
    assert result["todo_only"] is True
    assert result["failure_key"] == "gateway-todo-writer-loses-findings"
    assert result["post_write"]["verified"] is True
    assert "receipt" in result
    assert "## Runtime/tooling intake — 2026-09-07" in content
    assert "**Gateway TODO writer loses findings.**" in content
    assert "<!-- gateway-todo-key: gateway-todo-writer-loses-findings -->" in content
    assert "**Observed behavior:** Operators paste backlog findings" in content
    assert "**Acceptance:** Duplicate titles are refused" in content


def test_duplicate_checkbox_creation_is_refused(registry):
    workspace_registry, project = registry
    upsert_todo_backlog_entry(
        project_id=PROJECT_ID,
        registry=workspace_registry,
        **_entry_kwargs(),
    )
    before = (project / "TODO.md").read_text(encoding="utf-8")

    with pytest.raises(TodoBacklogError) as excinfo:
        upsert_todo_backlog_entry(
            project_id=PROJECT_ID,
            registry=workspace_registry,
            **_entry_kwargs(observed_behavior="same failure observed again"),
        )

    after = (project / "TODO.md").read_text(encoding="utf-8")
    assert excinfo.value.code == "ALREADY_EXISTS"
    assert excinfo.value.details["action"] == "duplicate_refused"
    assert before == after
    assert after.count("gateway-todo-writer-loses-findings") == 1


def test_update_existing_appends_evidence_without_new_checkbox(registry):
    workspace_registry, project = registry

    result = upsert_todo_backlog_entry(
        project_id=PROJECT_ID,
        registry=workspace_registry,
        failure_mode="existing gateway failure",
        update_existing=True,
        **_entry_kwargs(
            title="Different wording for the same mode",
            observed_behavior="A second session reproduced the same gateway failure.",
            related_evidence="run #1234",
        ),
    )

    content = (project / "TODO.md").read_text(encoding="utf-8")
    assert result["action"] == "updated"
    assert result["matched_title"] == "Existing gateway failure"
    assert content.count("1. ⬜ **Existing gateway failure.**") == 1
    assert "**Update 2026-09-07:**" in content
    assert "A second session reproduced the same gateway failure." in content
    assert "run #1234" in content


def test_invalid_required_fields_fail_before_write(registry):
    workspace_registry, project = registry
    before = (project / "TODO.md").read_text(encoding="utf-8")

    with pytest.raises(TodoBacklogError) as excinfo:
        upsert_todo_backlog_entry(
            project_id=PROJECT_ID,
            registry=workspace_registry,
            **_entry_kwargs(severity="P9"),
        )

    assert excinfo.value.code == "INVALID_INPUT"
    assert (project / "TODO.md").read_text(encoding="utf-8") == before


def test_tool_mode_scope_and_safe_mode_visibility(monkeypatch: pytest.MonkeyPatch):
    from examples.mcp_server import tool_modes
    from examples.mcp_server.tool_scopes import TOOL_SCOPES, has_required_scope

    assert tool_modes.should_register_tool("todo_backlog_upsert", "standard")
    assert tool_modes.should_register_tool("todo_backlog_upsert", "full")
    assert tool_modes.should_register_tool("todo_backlog_upsert", "mcp_client")
    assert not tool_modes.should_register_tool("todo_backlog_upsert", "minimal")
    assert TOOL_SCOPES["todo_backlog_upsert"] == ["mcp:project"]
    assert not has_required_scope(["mcp:read"], "todo_backlog_upsert")
    assert has_required_scope(["mcp:project"], "todo_backlog_upsert")

    monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
    assert "todo_backlog_upsert" not in tool_modes.get_mcp_client_safe_tools()


def test_adapter_maps_duplicate_to_contract_error():
    duplicate = TodoBacklogError(
        "matching TODO entry already exists",
        code="ALREADY_EXISTS",
        details={"action": "duplicate_refused"},
    )
    registry = MagicMock()
    registry.project_info.return_value = {"root": "/workspace/writeable-project"}
    with patch(
        "examples.mcp_server.mcp_infra.adapters.workspace._server_workspace_registry",
        return_value=registry,
    ), patch("os.path.isdir", return_value=True), patch("os.access", return_value=True), patch(
        "app.workspace.todo_backlog.upsert_todo_backlog_entry",
        side_effect=duplicate,
    ):
        from examples.mcp_server.mcp_infra.adapters.workspace import gateway_todo_backlog_upsert

        result = gateway_todo_backlog_upsert(
            project_id="p",
            title="Gateway TODO writer loses findings",
            severity="P2",
            observed_behavior="observed",
            reproduction_steps="repro",
            expected_behavior="expected",
            impact="impact",
            acceptance_criteria="acceptance",
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "ALREADY_EXISTS"
    assert result["error"]["details"]["action"] == "duplicate_refused"
