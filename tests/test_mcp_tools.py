"""Tests for P5 MCP tools: scan_file and explain_pattern."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))


# ── gateway_scan_file tests ──────────────────────────────────────────────────

def test_scan_file_detects_destructive_patterns():
    """scan_file finds destructive commands in file content."""
    from examples.mcp_server.server import gateway_scan_file

    mock_content = (
        "echo hello\n"
        "rm -rf /\n"
        "kubectl delete namespace prod\n"
        "ls -la\n"
    )
    mock_result = {"ok": True, "result": {"content": mock_content}}

    with patch("examples.mcp_server.server.read_file", return_value=mock_result):
        resp = gateway_scan_file(project="test", path="test.sh")

    assert resp["ok"] is True
    result = resp["result"]
    assert result["path"] == "test.sh"
    assert result["lines_scanned"] == 4
    assert result["total"] >= 2
    findings_by_line = {f["line"]: f["pattern_name"] for f in result["findings"]}
    assert 2 in findings_by_line
    assert 3 in findings_by_line


def test_scan_file_safe_file_has_no_findings():
    """scan_file returns empty findings for a safe file."""
    from examples.mcp_server.server import gateway_scan_file

    mock_content = "echo hello\nls -la\npwd\nwhoami\n"
    mock_result = {"ok": True, "result": {"content": mock_content}}

    with patch("examples.mcp_server.server.read_file", return_value=mock_result):
        resp = gateway_scan_file(project="test", path="safe.sh")

    assert resp["ok"] is True
    assert resp["result"]["total"] == 0
    assert resp["result"]["lines_scanned"] == 4


def test_scan_file_propagates_read_error():
    """scan_file returns tool_error when read_file fails."""
    from examples.mcp_server.server import gateway_scan_file

    mock_result = {"ok": False, "error": {"code": "NOT_FOUND", "message": "File not found"}}

    with patch("examples.mcp_server.server.read_file", return_value=mock_result):
        resp = gateway_scan_file(project="test", path="nonexistent.sh")

    assert resp["ok"] is False
    assert resp["error"]["code"] == "READ_ERROR"
    assert "not found" in resp["error"]["message"].lower()


def test_scan_file_empty_content():
    """scan_file handles empty file content."""
    from examples.mcp_server.server import gateway_scan_file

    mock_result = {"ok": True, "result": {"content": ""}}

    with patch("examples.mcp_server.server.read_file", return_value=mock_result):
        resp = gateway_scan_file(project="test", path="empty.sh")

    assert resp["ok"] is True
    assert resp["result"]["total"] == 0
    assert resp["result"]["lines_scanned"] == 0


# ── gateway_explain_pattern tests ────────────────────────────────────────────

def test_explain_pattern_finds_known_pattern():
    """explain_pattern returns full details for a known pattern."""
    from examples.mcp_server.server import gateway_explain_pattern

    resp = gateway_explain_pattern("rm-rf-root")

    assert resp["ok"] is True
    result = resp["result"]
    assert result["name"] == "rm-rf-root"
    assert "severity" in result
    assert "reason" in result
    assert "description" in result
    assert "suggestions" in result
    assert "pack" in result
    assert "regex" in result


def test_explain_pattern_returns_regex():
    """explain_pattern returns the raw regex for a pattern."""
    from examples.mcp_server.server import gateway_explain_pattern

    resp = gateway_explain_pattern("rm-rf-root")

    assert resp["ok"] is True
    regex = resp["result"]["regex"]
    assert isinstance(regex, str)
    assert len(regex) > 0


def test_explain_pattern_not_found():
    """explain_pattern returns tool_error for unknown pattern."""
    from examples.mcp_server.server import gateway_explain_pattern

    resp = gateway_explain_pattern("nonexistent-pattern-name")

    assert resp["ok"] is False
    assert resp["error"]["code"] == "PATTERN_NOT_FOUND"
    assert "nonexistent-pattern-name" in resp["error"]["message"]


@pytest.mark.parametrize("name", [
    "rm-rf-root",
    "rm-force",
    "kubectl-delete-namespace",
    "aws-ec2-terminate",
    "mysql-drop-database",
    "git-push-force",
    "iptables-flush",
    "dd-wipe",
])
def test_explain_pattern_known_patterns(name):
    """explain_pattern finds several known patterns across packs."""
    from examples.mcp_server.server import gateway_explain_pattern

    resp = gateway_explain_pattern(name)
    assert resp["ok"] is True, f"Pattern {name} should be found"
    assert resp["result"]["name"] == name


# ── write_agent_task: local schema/enum fails before backend work (TODO #24) ──


def test_write_agent_task_invalid_workflow_phase_fails_before_backend_work(monkeypatch):
    """A malformed workflow_phase is a pure local enum error.

    It must fail at the tool boundary with a typed error and the allowed
    values, before managed source publication, delivery-contract persistence,
    or any gateway/SSH work.
    """
    import examples.mcp_server.mcp_infra.adapters.agent as adapter
    import examples.mcp_server.server as server_mod
    from examples.mcp_server.agent_tasks import WORKFLOW_PHASES
    from examples.mcp_server.mcp_infra.tool_registry import _envelope_to_call_tool_result

    backend_events: list[str] = []

    def publish(project, base_ref):
        backend_events.append("publish-source")
        return None

    def record_contract(**kwargs):
        backend_events.append("delivery-contract")
        return {}

    client = MagicMock(name="gateway_client")
    monkeypatch.setattr(server_mod, "client", client)
    monkeypatch.setattr(adapter, "ensure_managed_source_bundle", publish)
    monkeypatch.setattr(adapter, "record_task_delivery_contract", record_contract)

    resp = adapter.gateway_write_agent_task(
        project="nod",
        task_id="schema-fastfail-001",
        agent="opencode",
        task="Do the thing",
        base_ref="a" * 40,
        workflow_phase="brainstorm",
    )

    assert resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_INPUT"
    assert resp["error"].get("retryable") is False
    message = resp["error"]["message"]
    assert "brainstorm" in message
    for phase in sorted(WORKFLOW_PHASES):
        assert phase in message

    assert backend_events == []
    assert client.mock_calls == []

    assert _envelope_to_call_tool_result(resp).isError is True

    for leak in ("Traceback", "MCP_AGENT_SOURCE_ROOT", "MCP_TASK_CANDIDATE_ROOT"):
        assert leak not in message


def test_write_agent_task_invalid_workflow_phase_rejected_before_dirty_publish(monkeypatch):
    """Same fast-fail contract on the dirty-worktree source path."""
    import examples.mcp_server.mcp_infra.adapters.agent as adapter
    from examples.mcp_server.agent_tasks import WORKFLOW_PHASES

    dirty_publish = MagicMock(name="dirty_publish")
    committed_publish = MagicMock(name="committed_publish")
    delivery_contract = MagicMock(name="delivery_contract", return_value={})
    write_task = MagicMock(
        name="write_task",
        return_value={"exit_code": 0, "stdout": "ok", "stderr": ""},
    )
    monkeypatch.setattr(adapter, "managed_workspace_path", lambda project, task_id: "/managed/x")
    monkeypatch.setattr(adapter, "ensure_dirty_worktree_review_bundle", dirty_publish)
    monkeypatch.setattr(adapter, "ensure_managed_source_bundle", committed_publish)
    monkeypatch.setattr(adapter, "record_task_delivery_contract", delivery_contract)
    monkeypatch.setattr(adapter, "_write_agent_task", write_task)

    resp = adapter.gateway_write_agent_task(
        project="nod",
        task_id="schema-fastfail-002",
        agent="opencode",
        task="Review current dirty worktree",
        base_ref="b" * 40,
        source_mode="dirty_worktree_snapshot",
        workflow_phase="brainstorm",
    )

    assert resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_INPUT"
    for phase in sorted(WORKFLOW_PHASES):
        assert phase in resp["error"]["message"]
    dirty_publish.assert_not_called()
    committed_publish.assert_not_called()
    delivery_contract.assert_not_called()
    write_task.assert_not_called()


def test_write_agent_task_valid_workflow_phase_keeps_existing_behavior(monkeypatch):
    """A valid workflow_phase still publishes source, records the delivery
    contract, and performs the remote task write in the existing order."""
    import examples.mcp_server.mcp_infra.adapters.agent as adapter
    import examples.mcp_server.server as server_mod

    events: list[str] = []
    client = MagicMock(name="gateway_client")
    client.execute_project_script.side_effect = lambda project, script: (
        events.append("write-task") or {"exit_code": 0, "stdout": "ok", "stderr": ""}
    )

    def publish(project, base_ref):
        events.append("publish-source")
        return None

    def record_contract(**kwargs):
        events.append("delivery-contract")
        return {}

    monkeypatch.setattr(server_mod, "client", client)
    monkeypatch.setattr(adapter, "ensure_managed_source_bundle", publish)
    monkeypatch.setattr(adapter, "record_task_delivery_contract", record_contract)

    resp = adapter.gateway_write_agent_task(
        project="nod",
        task_id="schema-fastfail-003",
        agent="opencode",
        task="Do the thing",
        base_ref="c" * 40,
        workflow_phase="verification",
    )

    assert resp["ok"] is True, resp
    assert events == ["publish-source", "delivery-contract", "write-task"]
