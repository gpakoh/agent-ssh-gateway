"""Tests for P5 MCP tools: scan_file and explain_pattern."""

import sys
from pathlib import Path
from unittest.mock import patch

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
