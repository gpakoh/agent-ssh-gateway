"""Tests for api_help documentation consistency.

Source of truth: the command_policy dict literal inside build_api_help()
in app/api_help.py.  Tests parse the AST of that file to extract actual
values and verify structural invariants — no duplicated expected data.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "app" / "api_help.py"


# ---------------------------------------------------------------------------
# Helpers — extract literal values from the build_api_help() source
# ---------------------------------------------------------------------------


def _extract_command_policy_dict() -> dict:
    """Return the command_policy dict literal from build_api_help() AST."""
    tree = ast.parse(SOURCE.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "build_api_help":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Dict):
                continue
            for i, key in enumerate(child.keys):
                if isinstance(key, ast.Constant) and key.value == "command_policy":
                    return ast.literal_eval(child.values[i])
    raise AssertionError("command_policy dict not found in build_api_help()")


CP = _extract_command_policy_dict()

# Pre-extract commonly tested values
ENDPOINTS_GATED: list[str] = CP["endpoints_gated"]
MCP_NOTES: str = CP["mcp_notes"]
PROFILES: dict[str, str] = CP["profiles"]
MODES: dict[str, str] = CP["modes"]
RESPONSE_CONTRACT: dict[str, str] = CP["response_contract"]
INTERACTION_NOTE: str = CP["interaction_with_workspace_readonly"]

# Canonical list of command endpoints the policy MUST gate.
# The test verifies this list matches the source — if someone removes an
# endpoint from the source, this test catches the drift.
REQUIRED_ENDPOINTS = [
    "POST /api/ssh/execute",
    "POST /api/ssh/execute-argv",
    "WS /api/ssh/execute/stream",
    "POST /api/jobs/run",
    "POST /api/bulk/execute",
    "POST /api/batch/execute",
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCommandPolicyDocs:
    """Verify command_policy docs are structurally complete and accurate."""

    # -- endpoints_gated ---------------------------------------------------

    def test_endpoints_gated_count(self):
        """Exactly 6 endpoints must be listed."""
        assert len(ENDPOINTS_GATED) == 6

    def test_endpoints_gated_includes_all_required(self):
        """Every canonical endpoint appears as a prefix in the real source."""
        for ep in REQUIRED_ENDPOINTS:
            assert any(entry.startswith(ep) for entry in ENDPOINTS_GATED), (
                f"{ep!r} missing from endpoints_gated"
            )

    def test_endpoints_gated_no_fakes(self):
        """No endpoint in the source is a made-up placeholder."""
        for ep in ENDPOINTS_GATED:
            assert ep.startswith(("POST ", "WS ", "GET ", "PATCH ")), (
                f"endpoint {ep!r} does not look like a real route"
            )

    # -- mcp_notes --------------------------------------------------------

    def test_mcp_notes_mentions_local_allowlist(self):
        """MCP-local allowlist (validate_readonly_command) must be documented."""
        assert "validate_readonly_command" in MCP_NOTES

    def test_mcp_notes_mentions_server_side_policy(self):
        """Server-side command policy must be referenced."""
        assert "server-side" in MCP_NOTES.lower() or "command policy" in MCP_NOTES.lower()

    def test_mcp_notes_no_unfounded_consolidation_claim(self):
        """Consolidation must not be claimed as done — only planned."""
        lower = MCP_NOTES.lower()
        if "consolidat" in lower:
            assert "planned" in lower, (
                "mcp_notes mentions consolidation without qualifying it as planned"
            )

    # -- structural completeness -------------------------------------------

    def test_modes_dict_has_three_keys(self):
        """Exactly 3 modes: off, audit, enforce."""
        assert set(MODES.keys()) == {"off", "audit", "enforce"}

    def test_profiles_dict_has_six_keys(self):
        """Exactly 6 profiles."""
        assert len(PROFILES) == 6

    def test_response_contract_has_rest_and_websocket(self):
        """Both REST and WebSocket contracts documented."""
        assert "rest" in RESPONSE_CONTRACT
        assert "websocket" in RESPONSE_CONTRACT

    def test_rest_contract_mentions_forbidden(self):
        """REST 403 contract must mention FORBIDDEN code."""
        assert "FORBIDDEN" in RESPONSE_CONTRACT["rest"]

    def test_websocket_contract_mentions_command_policy_denied(self):
        """WebSocket contract must mention COMMAND_POLICY_DENIED."""
        assert "COMMAND_POLICY_DENIED" in RESPONSE_CONTRACT["websocket"]

    def test_interaction_with_workspace_readonly_exists(self):
        """The cross-reference to WORKSPACE_READONLY must be present."""
        assert len(INTERACTION_NOTE) > 20
        assert "WORKSPACE_READONLY" in INTERACTION_NOTE

    def test_no_consolidation_as_current_behaviour(self):
        """Consolidation must not be described as current/implemented."""
        full_text = f"{MCP_NOTES} {INTERACTION_NOTE}".lower()
        assert "consolidated" not in full_text or "planned" in full_text
