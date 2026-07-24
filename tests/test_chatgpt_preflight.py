"""Tests for ChatGPT runtime preflight and env template."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestChatGPTSafeEnvTemplate:
    """Verify the env template is safe and gitignored."""

    def test_template_file_exists(self):
        path = ROOT / "examples" / "mcp_server" / "chatgpt.safe.env.example"
        assert path.is_file(), f"Template not found: {path}"

    def test_template_contains_only_placeholders(self):
        content = (ROOT / "examples" / "mcp_server" / "chatgpt.safe.env.example").read_text()
        assert "localhost:8085" not in content or "<gateway-url>" in content
        assert "<agent-token>" in content
        assert "MCP_CHATGPT_SAFE_MODE=true" in content
        assert "MCP_GATEWAY_TOOL_MODE=chatgpt" in content

    def test_private_env_is_gitignored(self):
        gitignore = (ROOT / ".gitignore").read_text()
        assert "chatgpt.safe.env" in gitignore

    def test_template_no_master_key_as_runtime(self):
        """Template says NEVER use master key as MCP runtime credential (not a positive assertion)."""
        content = (ROOT / "examples" / "mcp_server" / "chatgpt.safe.env.example").read_text()
        # Must NOT say to use master key positively
        assert "use master key" not in content.lower() or "NEVER use master key" in content

    def test_template_has_safe_mode(self):
        content = (ROOT / "examples" / "mcp_server" / "chatgpt.safe.env.example").read_text()
        assert "MCP_CHATGPT_SAFE_MODE=true" in content
        assert "MCP_GATEWAY_TOOL_MODE=chatgpt" in content


class TestChatGPTPreflight:
    """Test the preflight script."""

    def _run_preflight(self, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
        """Run preflight with custom env."""
        script = ROOT / "scripts" / "mcp_chatgpt_runtime_preflight.py"
        env = os.environ.copy()
        # Clear all relevant env vars first
        for key in ("GATEWAY_URL", "GATEWAY_AGENT_TOKEN", "MCP_GATEWAY_TOOL_MODE",
                     "MCP_CHATGPT_SAFE_MODE", "MCP_ACCESS_PROFILE"):
            env.pop(key, None)
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_fails_without_safe_mode(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "dummy",
            "MCP_GATEWAY_TOOL_MODE": "chatgpt",
        })
        assert result.returncode == 1
        assert "MCP_CHATGPT_SAFE_MODE" in result.stdout

    def test_fails_with_wrong_mode(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "dummy",
            "MCP_GATEWAY_TOOL_MODE": "standard",
            "MCP_CHATGPT_SAFE_MODE": "true",
        })
        assert result.returncode == 1
        assert "MCP_GATEWAY_TOOL_MODE" in result.stdout

    def test_passes_with_correct_config(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "dummy-token-for-test",
            "MCP_GATEWAY_TOOL_MODE": "chatgpt",
            "MCP_CHATGPT_SAFE_MODE": "true",
        })
        assert result.returncode == 0
        assert "dummy-token-for-test" not in result.stdout, "Token must not be printed"
        assert "passed" in result.stdout.lower()
        assert "failed" in result.stdout.lower() or "0 failed" in result.stdout

    def test_does_not_print_token(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "super-secret-token-value-12345",
            "MCP_GATEWAY_TOOL_MODE": "chatgpt",
            "MCP_CHATGPT_SAFE_MODE": "true",
        })
        assert "super-secret-token-value-12345" not in result.stdout
        assert "super-secret-token-value-12345" not in result.stderr

    def test_fails_without_token(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "MCP_GATEWAY_TOOL_MODE": "chatgpt",
            "MCP_CHATGPT_SAFE_MODE": "true",
        })
        assert result.returncode == 1

    def test_safe_mode_excludes_blocked_tools(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "test-token",
            "MCP_GATEWAY_TOOL_MODE": "chatgpt",
            "MCP_CHATGPT_SAFE_MODE": "true",
        })
        assert "project_run_opencode" in result.stdout
        assert "excluded" in result.stdout.lower()

    def test_docs_no_master_key_as_runtime_credential(self):
        """CHATGPT_TOOL_ATTACH.md must not say to use master key as MCP runtime."""
        docs_path = ROOT / "docs" / "operations" / "CHATGPT_TOOL_ATTACH.md"
        content = docs_path.read_text()
        # Find the MCP server start command section
        assert "GATEWAY_AGENT_TOKEN" in content
        assert "never master" in content.lower() or "never use the master" in content.lower()
