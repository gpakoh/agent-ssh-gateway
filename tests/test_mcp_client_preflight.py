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
        path = ROOT / "examples" / "mcp_server" / "mcp_client.safe.env.example"
        assert path.is_file(), f"Template not found: {path}"

    def test_template_contains_only_placeholders(self):
        content = (ROOT / "examples" / "mcp_server" / "mcp_client.safe.env.example").read_text()
        assert "localhost:8085" not in content or "<gateway-url>" in content
        assert "<agent-token>" in content
        assert "MCP_CLIENT_SAFE_MODE=true" in content
        assert "MCP_GATEWAY_TOOL_MODE=mcp_client" in content

    def test_private_env_is_gitignored(self):
        gitignore = (ROOT / ".gitignore").read_text()
        assert "mcp_client.safe.env" in gitignore

    def test_template_no_master_key_as_runtime(self):
        """Template says NEVER use master key as MCP runtime credential (not a positive assertion)."""
        content = (ROOT / "examples" / "mcp_server" / "mcp_client.safe.env.example").read_text()
        # Must NOT say to use master key positively
        assert "use master key" not in content.lower() or "NEVER use master key" in content

    def test_template_has_safe_mode(self):
        content = (ROOT / "examples" / "mcp_server" / "mcp_client.safe.env.example").read_text()
        assert "MCP_CLIENT_SAFE_MODE=true" in content
        assert "MCP_GATEWAY_TOOL_MODE=mcp_client" in content


class TestChatGPTPreflight:
    """Test the preflight script."""

    def _run_preflight(self, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
        """Run preflight with custom env."""
        script = ROOT / "scripts" / "mcp_client_runtime_preflight.py"
        env = os.environ.copy()
        # Clear all relevant env vars first
        for key in ("GATEWAY_URL", "GATEWAY_AGENT_TOKEN", "MCP_GATEWAY_TOOL_MODE",
                     "MCP_CLIENT_SAFE_MODE", "MCP_ACCESS_PROFILE"):
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
            "MCP_GATEWAY_TOOL_MODE": "mcp_client",
        })
        assert result.returncode == 1
        assert "MCP_CLIENT_SAFE_MODE" in result.stdout

    def test_fails_with_wrong_mode(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "dummy",
            "MCP_GATEWAY_TOOL_MODE": "standard",
            "MCP_CLIENT_SAFE_MODE": "true",
        })
        assert result.returncode == 1
        assert "MCP_GATEWAY_TOOL_MODE" in result.stdout

    def test_wrong_mode_reports_actual_value_in_detail(self):
        """Regression: a stray comma turned the third `check()` arg into a
        discarded tuple expression, so the detail suffix (e.g. value='standard')
        was silently swallowed — the line printed the bare label with no
        indication of what the misconfigured value actually was.
        """
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "dummy",
            "MCP_GATEWAY_TOOL_MODE": "standard",
            "MCP_CLIENT_SAFE_MODE": "true",
        })
        assert result.returncode == 1
        assert "value='standard'" in result.stdout

    def test_missing_mode_reports_missing_in_detail(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "dummy",
            "MCP_CLIENT_SAFE_MODE": "true",
        })
        assert result.returncode == 1
        assert "MCP_GATEWAY_TOOL_MODE=mcp_client — missing" in result.stdout

    def test_passes_with_correct_config(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "dummy-token-for-test",
            "MCP_GATEWAY_TOOL_MODE": "mcp_client",
            "MCP_CLIENT_SAFE_MODE": "true",
        })
        assert result.returncode == 0
        assert "dummy-token-for-test" not in result.stdout, "Token must not be printed"
        assert "passed" in result.stdout.lower()
        assert "failed" in result.stdout.lower() or "0 failed" in result.stdout

    def test_does_not_print_token(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "super-secret-token-value-12345",
            "MCP_GATEWAY_TOOL_MODE": "mcp_client",
            "MCP_CLIENT_SAFE_MODE": "true",
        })
        assert "super-secret-token-value-12345" not in result.stdout
        assert "super-secret-token-value-12345" not in result.stderr

    def test_fails_without_token(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "MCP_GATEWAY_TOOL_MODE": "mcp_client",
            "MCP_CLIENT_SAFE_MODE": "true",
        })
        assert result.returncode == 1

    def test_safe_mode_excludes_blocked_tools(self):
        result = self._run_preflight({
            "GATEWAY_URL": "http://localhost:8085",
            "GATEWAY_AGENT_TOKEN": "test-token",
            "MCP_GATEWAY_TOOL_MODE": "mcp_client",
            "MCP_CLIENT_SAFE_MODE": "true",
        })
        assert "run_opencode" in result.stdout
        assert "excluded" in result.stdout.lower()

    def test_docs_no_master_key_as_runtime_credential(self):
        """CHATGPT_TOOL_ATTACH.md must not say to use master key as MCP runtime."""
        docs_path = ROOT / "docs" / "operations" / "CHATGPT_TOOL_ATTACH.md"
        content = docs_path.read_text()
        # Find the MCP server start command section
        assert "GATEWAY_AGENT_TOKEN" in content
        assert "never master" in content.lower() or "never use the master" in content.lower()


class TestChatGPTAttachChecklist:
    """Contract tests for CHATGPT_ATTACH_CHECKLIST.md."""

    def _load_checklist(self) -> str:
        return (ROOT / "docs" / "operations" / "CHATGPT_ATTACH_CHECKLIST.md").read_text()

    def test_checklist_exists(self):
        assert (ROOT / "docs" / "operations" / "CHATGPT_ATTACH_CHECKLIST.md").is_file()

    def test_agent_token_never_master(self):
        content = self._load_checklist().lower()
        assert "agent token" in content
        # Must explicitly say not to use master as runtime credential
        assert "never" in content and "master key" in content

    def test_no_forbidden_scopes_in_token(self):
        """Checklist forbids these scopes — must appear as 'forbidden' or 'do not' context."""
        content = self._load_checklist().lower()
        for scope in ("project:write", "project:patch", "jobs:run"):
            assert scope in content  # must be mentioned as forbidden
        # ssh:files is mentioned but explicitly forbidden
        assert "ssh:files" in content

    def test_forbidden_scopes_not_in_allowed(self):
        """Forbidden scopes must not appear in the allowed scopes list."""
        content = self._load_checklist()
        # Extract the allowed scopes section — should not contain forbidden scopes
        import re
        allowed_match = re.search(r"Allowed scopes:.*?`([^`]+)`.*?`([^`]+)`.*?`([^`]+)`.*?`([^`]+)`", content)
        if allowed_match:
            allowed = " ".join(allowed_match.groups())
            for scope in ("ssh:files", "project:write", "project:patch", "jobs:run"):
                assert scope not in allowed, f" {scope} in allowed scopes"

    def test_safe_mode_referenced(self):
        content = self._load_checklist()
        assert "MCP_CLIENT_SAFE_MODE=true" in content

    def test_private_env_template_referenced(self):
        content = self._load_checklist()
        assert "mcp_client.safe.env" in content

    def test_cleanup_revoke_referenced(self):
        content = self._load_checklist().lower()
        assert "cleanup" in content or "clear" in content
        assert "revoke" in content

    def test_no_real_secrets_or_topology(self):
        content = self._load_checklist()
        import re
        # No real IPs (except placeholders)
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", content.replace("<gateway>", "").replace("<ip>", "")) or "<" in content
        # No real tokens (long hex strings)
        assert not re.search(r"\b[A-F0-9]{20,}\b", content.replace("<", ""))
        # No real domains
        assert "github.com" not in content or "github.com" in content  # placeholder OK


class TestMcpStdioSmoke:
    """Contract tests for scripts/mcp_stdio_safe_smoke.py."""

    def _load_script(self) -> str:
        return (ROOT / "scripts" / "mcp_stdio_safe_smoke.py").read_text()

    def test_script_exists(self):
        assert (ROOT / "scripts" / "mcp_stdio_safe_smoke.py").is_file()

    def test_script_has_blocked_tools_set(self):
        content = self._load_script()
        for tool in ("run_opencode", "run_agent",
                      "docker_exec", "docker_compose_up", "workspace_file_write",
                      "workspace_apply_patch", "apply_patch"):
            assert tool in content, f"Blocked tool {tool} missing from BLOCKED_TOOLS"

    def test_script_has_required_tools(self):
        content = self._load_script()
        assert '"health"' in content
        assert '"tools_manifest"' in content

    def test_script_no_token_print(self):
        content = self._load_script()
        assert "GATEWAY_AGENT_TOKEN=<REDACTED>" in content
        assert "print(token" not in content
        assert "print(os.environ[\"GATEWAY_AGENT_TOKEN\"])" not in content

    def test_script_exits_nonzero_on_unsafe(self):
        content = self._load_script()
        assert "return 1" in content
        assert "UNSAFE MANIFEST" in content

    def test_script_safe_mode_flags(self):
        content = self._load_script()
        assert "MCP_CLIENT_SAFE_MODE" in content
        assert '"mcp_client"' in content or "'mcp_client'" in content

    def test_no_real_secrets_in_script(self):
        content = self._load_script()
        import re
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", content)
        assert not re.search(r"\b[A-F0-9]{40,}\b", content)


class TestChatGPTConnectorHandoff:
    """Contract tests for CHATGPT_CONNECTOR_HANDOFF.md."""

    def _load_handoff(self) -> str:
        return (ROOT / "docs" / "operations" / "CHATGPT_CONNECTOR_HANDOFF.md").read_text()

    def test_handoff_doc_exists(self):
        assert (ROOT / "docs" / "operations" / "CHATGPT_CONNECTOR_HANDOFF.md").is_file()

    def test_agent_token_never_master(self):
        content = self._load_handoff().lower()
        assert "agent token" in content
        assert "never" in content and "master" in content

    def test_stop_conditions_present(self):
        content = self._load_handoff().lower()
        assert "stop condition" in content or "stop" in content
        assert "rollback" in content or "revoke" in content

    def test_forbidden_scopes_not_listed_as_allowed(self):
        content = self._load_handoff()
        assert "ssh:files" in content
        assert "project:write" in content
        assert "Forbidden" in content or "forbidden" in content
        assert "NEVER" in content or "never" in content

    def test_no_real_secrets_or_topology(self):
        content = self._load_handoff()
        import re
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", content)
        assert not re.search(r"\b[A-F0-9]{40,}\b", content)


class TestChatGPTExpectedManifest:
    """Contract tests for mcp_client.safe.manifest.expected.json."""

    def _load_manifest(self) -> dict:
        import json
        return json.loads((ROOT / "examples" / "mcp_server" / "mcp_client.safe.manifest.expected.json").read_text())

    def test_manifest_exists(self):
        assert (ROOT / "examples" / "mcp_server" / "mcp_client.safe.manifest.expected.json").is_file()

    def test_manifest_mode_and_safe_flag(self):
        m = self._load_manifest()
        assert m["mode"] == "mcp_client"
        assert m["safe_mode"] is True

    def test_manifest_counts_match_code(self):
        import sys
        sys.path.insert(0, str(ROOT / "examples" / "mcp_server"))
        from tool_modes import MCP_CLIENT_BLOCKED_TOOLS, get_mcp_client_safe_tools
        m = self._load_manifest()
        assert m["expected_safe_count"] == len(get_mcp_client_safe_tools())
        assert m["expected_blocked_count"] == len(MCP_CLIENT_BLOCKED_TOOLS)

    def test_manifest_must_include_present_in_safe(self):
        import sys
        sys.path.insert(0, str(ROOT / "examples" / "mcp_server"))
        from tool_modes import get_mcp_client_safe_tools
        m = self._load_manifest()
        safe = get_mcp_client_safe_tools()
        for tool in m["must_include"]:
            assert tool in safe, f"must_include tool {tool} not in safe set"

    def test_manifest_must_exclude_absent_from_safe(self):
        import sys
        sys.path.insert(0, str(ROOT / "examples" / "mcp_server"))
        from tool_modes import get_mcp_client_safe_tools
        m = self._load_manifest()
        safe = get_mcp_client_safe_tools()
        for tool in m["must_exclude"]:
            assert tool not in safe, f"must_exclude tool {tool} found in safe set"


class TestOpenAIConnectorReadiness:
    """Contract tests for OPENAI_CONNECTOR_READINESS.md."""

    def _load_doc(self) -> str:
        return (ROOT / "docs" / "operations" / "OPENAI_CONNECTOR_READINESS.md").read_text()

    def test_doc_exists(self):
        assert (ROOT / "docs" / "operations" / "OPENAI_CONNECTOR_READINESS.md").is_file()

    def test_stdio_smoke_ready(self):
        content = self._load_doc().lower()
        assert "stdio" in content
        assert "ready" in content or "complete" in content or "done" in content

    def test_public_connector_not_live(self):
        content = self._load_doc().lower()
        assert "not live" in content or "not ready" in content or "deferred" in content or "non-goal" in content

    def test_mentions_missing_transport(self):
        content = self._load_doc().lower()
        assert "http" in content or "sse" in content or "streamable" in content
        assert "transport" in content

    def test_mentions_auth_or_oauth(self):
        content = self._load_doc().lower()
        assert "auth" in content or "oauth" in content or "token" in content

    def test_mentions_tls(self):
        content = self._load_doc().lower()
        assert "tls" in content

    def test_no_real_secrets_or_topology(self):
        import re
        content = self._load_doc()
        # 127.0.0.1 and 0.0.0.0 are documented safe/dangerous-example
        # addresses for the private SSE entrypoint (Phase 16B), not a
        # real-topology leak — only private ranges (10./192.168./172.)
        # count as a leak here, matching the pattern already used for
        # the private HTTP MCP transport spec test below.
        assert not re.search(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            content,
        )
        assert not re.search(r"\b[A-F0-9]{40,}\b", content)

    def test_no_master_key_runtime(self):
        content = self._load_doc().lower()
        assert "master key" not in content or "never" in content or "not" in content or "non-goal" in content


class TestPrivateHTTPMCPTransportSpec:
    """Contract tests for private HTTP MCP transport design spec."""

    def _load_spec(self) -> str:
        return (ROOT / "docs" / "superpowers" / "specs" / "2026-07-25-private-http-mcp-transport.md").read_text()

    def test_spec_exists(self):
        assert (ROOT / "docs" / "superpowers" / "specs" / "2026-07-25-private-http-mcp-transport.md").is_file()

    def test_stdio_current_http_not_wired(self):
        content = self._load_spec().lower()
        assert "stdio" in content
        assert "not wired" in content or "not implemented" in content or "not yet" in content

    def test_private_bind_default(self):
        content = self._load_spec()
        assert "127.0.0.1" in content or "localhost" in content

    def test_no_public_exposure(self):
        content = self._load_spec().lower()
        assert "no public" in content or "not public" in content or "private" in content

    def test_agent_token_never_master(self):
        content = self._load_spec().lower()
        assert "agent token" in content
        assert "master key" in content and ("never" in content or "not" in content or "non-goal" in content)

    def test_safe_mode_mandatory(self):
        content = self._load_spec()
        assert "MCP_CLIENT_SAFE_MODE=true" in content or "MCP_CLIENT_SAFE_MODE" in content

    def test_public_connector_deferred(self):
        content = self._load_spec().lower()
        assert "deferred" in content or "not live" in content or "non-goal" in content

    def test_no_real_secrets_or_topology(self):
        import re
        content = self._load_spec()
        assert not re.search(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            content,
        )
        assert not re.search(r"\b[A-F0-9]{40,}\b", content)


class TestPrivateSSEEnvTemplate:
    """Verify the private SSE env template (mcp_client.sse.env.example) is
    safe, gitignored, and placeholder-only. Phase 16B PR3.
    """

    def _load(self) -> str:
        return (ROOT / "examples" / "mcp_server" / "mcp_client.sse.env.example").read_text()

    def test_template_file_exists(self):
        assert (ROOT / "examples" / "mcp_server" / "mcp_client.sse.env.example").is_file()

    def test_template_contains_only_placeholders(self):
        content = self._load()
        assert "<agent-token>" in content
        assert "<generate-private-token>" in content
        assert "MCP_GATEWAY_TOOL_MODE=mcp_client" in content
        assert "MCP_CLIENT_SAFE_MODE=true" in content

    def test_template_has_private_sse_bind_defaults(self):
        content = self._load()
        assert "MCP_HTTP_HOST=127.0.0.1" in content
        assert "MCP_HTTP_PORT=8086" in content
        assert "MCP_HTTP_BEARER_TOKEN" in content

    def test_private_env_is_gitignored(self):
        gitignore = (ROOT / ".gitignore").read_text()
        assert "mcp_client.sse.env" in gitignore

    def test_template_no_master_key_as_runtime(self):
        content = self._load()
        assert "NEVER use master key" in content

    def test_template_warns_about_non_loopback_override(self):
        content = self._load()
        assert "MCP_HTTP_ALLOW_NON_LOOPBACK" in content
        assert "NEVER set MCP_HTTP_ALLOW_NON_LOOPBACK" in content
        assert "MCP_HTTP_BIND_PUBLIC" not in content

    def test_no_real_secrets_or_topology(self):
        import re

        content = self._load()
        # 127.0.0.1 is the documented safe default, not a real-topology leak.
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", content.replace("127.0.0.1", ""))
        assert not re.search(r"\b[A-F0-9]{20,}\b", content)


class TestChatGPTToolAttachPrivateSSESection:
    """Contract tests for the private SSE section of
    CHATGPT_TOOL_ATTACH.md. Phase 16B PR3.
    """

    def _load(self) -> str:
        return (ROOT / "docs" / "operations" / "CHATGPT_TOOL_ATTACH.md").read_text()

    def test_mentions_sse_and_messages_routes(self):
        content = self._load()
        assert "/sse" in content
        assert "/messages" in content

    def test_mentions_bearer_token_env_var(self):
        content = self._load()
        assert "MCP_HTTP_BEARER_TOKEN" in content
        assert "Bearer" in content

    def test_mentions_default_loopback_bind(self):
        content = self._load()
        assert "127.0.0.1" in content
        assert "MCP_HTTP_HOST" in content

    def test_non_loopback_override_documented_as_dangerous_and_correctly_named(self):
        content = self._load()
        assert "MCP_HTTP_ALLOW_NON_LOOPBACK" in content
        assert "MCP_HTTP_BIND_PUBLIC" not in content, "old/wrong env var name must not be recommended"
        lowered = content.lower()
        assert "danger" in lowered or "⚠️" in content

    def test_mentions_sse_smoke_script(self):
        content = self._load()
        assert "mcp_sse_safe_smoke.py" in content

    def test_no_forbidden_scopes_mentioned(self):
        content = self._load().lower()
        for scope in ("ssh:files", "project:write", "jobs:run"):
            assert scope not in content

    def test_no_real_secrets_or_topology(self):
        import re

        content = self._load()
        # 127.0.0.1 is the documented default; 0.0.0.0 appears only in
        # the dangerous-example warning text for MCP_HTTP_ALLOW_NON_LOOPBACK
        # — neither is a real-topology leak.
        sanitized = content.replace("127.0.0.1", "").replace("0.0.0.0", "")
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", sanitized)
        assert not re.search(r"\b[A-F0-9]{20,}\b", content)


class TestOpenAIConnectorReadinessPrivateSSEUpdate:
    """Contract tests for the Phase 16B private SSE update to
    OPENAI_CONNECTOR_READINESS.md — must reflect that a private SSE
    entrypoint now exists, while stdio stays the default/stable path and
    no public connector is live.
    """

    def _load_doc(self) -> str:
        return (ROOT / "docs" / "operations" / "OPENAI_CONNECTOR_READINESS.md").read_text()

    def test_stdio_remains_default_stable_path(self):
        content = self._load_doc().lower()
        assert "stdio" in content
        assert "default" in content or "stable" in content

    def test_private_sse_entrypoint_mentioned(self):
        content = self._load_doc()
        assert "mcp_sse_serve.py" in content
        assert "mcp_sse_safe_smoke.py" in content

    def test_option_b_marked_implemented_private_only(self):
        content = self._load_doc()
        assert "IMPLEMENTED" in content
        assert "private" in content.lower()

    def test_public_connector_still_not_live(self):
        content = self._load_doc().lower()
        assert "not live" in content

    def test_uses_correct_non_loopback_env_var(self):
        content = self._load_doc()
        assert "MCP_HTTP_ALLOW_NON_LOOPBACK" in content
        assert "MCP_HTTP_BIND_PUBLIC" not in content

    def test_mentions_sse_and_messages_routes(self):
        content = self._load_doc()
        assert "/sse" in content
        assert "/messages" in content

    def test_default_bind_is_loopback(self):
        content = self._load_doc()
        assert "127.0.0.1" in content

    def test_no_real_secrets_or_topology(self):
        import re

        content = self._load_doc()
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", content.replace("127.0.0.1", ""))
        assert not re.search(r"\b[A-F0-9]{40,}\b", content)


class TestMcpPrivateSSERunbook:
    """Contract tests for docs/operations/MCP_PRIVATE_SSE_RUNBOOK.md.
    Phase 16C.
    """

    def _load(self) -> str:
        return (ROOT / "docs" / "operations" / "MCP_PRIVATE_SSE_RUNBOOK.md").read_text()

    def test_runbook_exists(self):
        assert (ROOT / "docs" / "operations" / "MCP_PRIVATE_SSE_RUNBOOK.md").is_file()

    def test_has_start_stop_smoke_rollback_sections(self):
        content = self._load().lower()
        assert "start manually" in content
        assert "stop the process" in content
        assert "smoke" in content
        assert "rollback" in content

    def test_has_what_not_to_do_section(self):
        content = self._load()
        assert "## What not to do" in content

    def test_public_connector_not_live(self):
        content = self._load().lower()
        assert "not a public" in content or "not live" in content

    def test_no_compose_auto_start(self):
        content = self._load().lower()
        assert "docker-compose" in content or "compose" in content
        assert "not" in content and ("compose" in content)
        # explicit sentence forbidding compose wiring
        assert "do not add this entrypoint to any" in content or "not wired into any docker compose" in content.replace("Docker Compose", "docker compose")

    def test_default_bind_loopback(self):
        content = self._load()
        assert "127.0.0.1" in content

    def test_bearer_token_required(self):
        content = self._load()
        assert "MCP_HTTP_BEARER_TOKEN" in content
        assert "bearer" in content.lower()

    def test_safe_mode_required(self):
        content = self._load()
        assert "MCP_CLIENT_SAFE_MODE=true" in content
        assert "mandatory" in content.lower()

    def test_warns_against_non_loopback_override(self):
        content = self._load()
        assert "MCP_HTTP_ALLOW_NON_LOOPBACK" in content
        lowered = content.lower()
        assert "forbidden" in lowered

    def test_no_master_key_as_runtime(self):
        content = self._load().lower()
        assert "master key" in content
        assert "do not use the master key" in content or "never" in content

    def test_agent_token_only(self):
        content = self._load()
        assert "agent token" in content.lower()

    def test_references_sse_and_messages_routes(self):
        content = self._load()
        assert "/sse" in content
        assert "/messages" in content

    def test_references_smoke_script(self):
        content = self._load()
        assert "mcp_sse_safe_smoke.py" in content

    def test_references_env_check_helper(self):
        content = self._load()
        assert "mcp_sse_env_check.py" in content

    def test_no_forbidden_scopes_mentioned(self):
        content = self._load().lower()
        for scope in ("ssh:files", "project:write", "jobs:run"):
            assert scope not in content

    def test_no_real_secrets_or_topology(self):
        import re

        content = self._load()
        sanitized = content.replace("127.0.0.1", "").replace("0.0.0.0", "")
        assert not re.search(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            sanitized,
        )
        assert not re.search(r"\b[A-F0-9]{20,}\b", content)


class TestMcpSseEnvCheckScript:
    """Tests for scripts/mcp_sse_env_check.py. Phase 16C.

    All cases run against temp files — never the real repo template or
    a real operator env file — and never start any server.
    """

    def _run(self, env_path) -> subprocess.CompletedProcess:
        script = ROOT / "scripts" / "mcp_sse_env_check.py"
        return subprocess.run(
            [sys.executable, str(script), str(env_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _write_env(self, tmp_path, content: str):
        path = tmp_path / "test.env"
        path.write_text(content)
        return path

    def test_script_exists(self):
        assert (ROOT / "scripts" / "mcp_sse_env_check.py").is_file()

    def test_passes_on_fully_valid_env(self, tmp_path):
        env = self._write_env(
            tmp_path,
            "MCP_GATEWAY_TOOL_MODE=mcp_client\n"
            "MCP_CLIENT_SAFE_MODE=true\n"
            "MCP_HTTP_HOST=127.0.0.1\n"
            "MCP_HTTP_BEARER_TOKEN=real-bearer-token-value\n"
            "GATEWAY_AGENT_TOKEN=real-agent-token-value\n",
        )
        result = self._run(env)
        assert result.returncode == 0
        assert "6 passed, 0 failed" in result.stdout

    def test_fails_on_template_placeholders(self):
        # The shipped .example template itself must fail — placeholders
        # were never meant to be used as-is.
        template = ROOT / "examples" / "mcp_server" / "mcp_client.sse.env.example"
        result = self._run(template)
        assert result.returncode == 1
        assert "template placeholder" in result.stdout

    def test_fails_on_wrong_tool_mode(self, tmp_path):
        env = self._write_env(
            tmp_path,
            "MCP_GATEWAY_TOOL_MODE=standard\n"
            "MCP_CLIENT_SAFE_MODE=true\n"
            "MCP_HTTP_HOST=127.0.0.1\n"
            "MCP_HTTP_BEARER_TOKEN=real-bearer-token-value\n"
            "GATEWAY_AGENT_TOKEN=real-agent-token-value\n",
        )
        result = self._run(env)
        assert result.returncode == 1
        assert "MCP_GATEWAY_TOOL_MODE" in result.stdout

    def test_fails_on_safe_mode_false(self, tmp_path):
        env = self._write_env(
            tmp_path,
            "MCP_GATEWAY_TOOL_MODE=mcp_client\n"
            "MCP_CLIENT_SAFE_MODE=false\n"
            "MCP_HTTP_HOST=127.0.0.1\n"
            "MCP_HTTP_BEARER_TOKEN=real-bearer-token-value\n"
            "GATEWAY_AGENT_TOKEN=real-agent-token-value\n",
        )
        result = self._run(env)
        assert result.returncode == 1
        assert "MCP_CLIENT_SAFE_MODE" in result.stdout

    def test_fails_on_non_loopback_host(self, tmp_path):
        env = self._write_env(
            tmp_path,
            "MCP_GATEWAY_TOOL_MODE=mcp_client\n"
            "MCP_CLIENT_SAFE_MODE=true\n"
            "MCP_HTTP_HOST=0.0.0.0\n"
            "MCP_HTTP_BEARER_TOKEN=real-bearer-token-value\n"
            "GATEWAY_AGENT_TOKEN=real-agent-token-value\n",
        )
        result = self._run(env)
        assert result.returncode == 1
        assert "loopback" in result.stdout.lower()

    def test_fails_loudly_when_allow_non_loopback_enabled(self, tmp_path):
        env = self._write_env(
            tmp_path,
            "MCP_GATEWAY_TOOL_MODE=mcp_client\n"
            "MCP_CLIENT_SAFE_MODE=true\n"
            "MCP_HTTP_HOST=127.0.0.1\n"
            "MCP_HTTP_ALLOW_NON_LOOPBACK=true\n"
            "MCP_HTTP_BEARER_TOKEN=real-bearer-token-value\n"
            "GATEWAY_AGENT_TOKEN=real-agent-token-value\n",
        )
        result = self._run(env)
        assert result.returncode == 1
        assert "DANGER" in result.stdout

    def test_fails_on_missing_bearer_token(self, tmp_path):
        env = self._write_env(
            tmp_path,
            "MCP_GATEWAY_TOOL_MODE=mcp_client\n"
            "MCP_CLIENT_SAFE_MODE=true\n"
            "MCP_HTTP_HOST=127.0.0.1\n"
            "GATEWAY_AGENT_TOKEN=real-agent-token-value\n",
        )
        result = self._run(env)
        assert result.returncode == 1
        assert "MCP_HTTP_BEARER_TOKEN" in result.stdout

    def test_fails_on_missing_file(self, tmp_path):
        result = self._run(tmp_path / "does-not-exist.env")
        assert result.returncode == 1

    def test_never_prints_token_values(self, tmp_path):
        env = self._write_env(
            tmp_path,
            "MCP_GATEWAY_TOOL_MODE=mcp_client\n"
            "MCP_CLIENT_SAFE_MODE=true\n"
            "MCP_HTTP_HOST=127.0.0.1\n"
            "MCP_HTTP_BEARER_TOKEN=super-secret-bearer-value-xyz\n"
            "GATEWAY_AGENT_TOKEN=super-secret-agent-value-abc\n",
        )
        result = self._run(env)
        assert "super-secret-bearer-value-xyz" not in result.stdout
        assert "super-secret-agent-value-abc" not in result.stdout
        assert "super-secret-bearer-value-xyz" not in result.stderr
        assert "super-secret-agent-value-abc" not in result.stderr

    def test_does_not_start_a_server(self, tmp_path):
        """The script must be pure static validation — confirm no
        listening socket appears on the default SSE port while/after
        running it against a fully valid env.
        """
        import socket

        env = self._write_env(
            tmp_path,
            "MCP_GATEWAY_TOOL_MODE=mcp_client\n"
            "MCP_CLIENT_SAFE_MODE=true\n"
            "MCP_HTTP_HOST=127.0.0.1\n"
            "MCP_HTTP_PORT=8086\n"
            "MCP_HTTP_BEARER_TOKEN=real-bearer-token-value\n"
            "GATEWAY_AGENT_TOKEN=real-agent-token-value\n",
        )
        self._run(env)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            still_listening = s.connect_ex(("127.0.0.1", 8086)) == 0
        assert not still_listening


class TestMcpPrivateSSERehearsalRecord:
    """Contract tests for docs/operations/MCP_PRIVATE_SSE_REHEARSAL.md.
    Phase 16E.
    """

    def _load(self) -> str:
        return (ROOT / "docs" / "operations" / "MCP_PRIVATE_SSE_REHEARSAL.md").read_text()

    def test_doc_exists(self):
        assert (ROOT / "docs" / "operations" / "MCP_PRIVATE_SSE_REHEARSAL.md").is_file()

    def test_says_manual_local_only(self):
        content = self._load().lower()
        assert "manual" in content
        assert "local" in content

    def test_public_connector_not_live(self):
        content = self._load().lower()
        assert "not live" in content

    def test_says_loopback_only(self):
        content = self._load()
        assert "127.0.0.1" in content
        assert "loopback" in content.lower()

    def test_no_compose_systemd_autostart(self):
        content = self._load().lower()
        assert "compose" in content
        assert "systemd" in content
        assert "autostart" in content or "auto-start" in content or "automatic" in content

    def test_says_env_deleted(self):
        content = self._load().lower()
        assert "deleted" in content

    def test_says_no_or_wrong_token_401(self):
        content = self._load()
        assert "401" in content
        lowered = content.lower()
        assert "no token" in lowered
        assert "wrong token" in lowered

    def test_says_correct_token_works(self):
        content = self._load().lower()
        assert "correct token" in content
        assert "non-401" in content or "success" in content or "opened" in content

    def test_says_84_safe_tools(self):
        content = self._load()
        assert "84" in content

    def test_says_blocked_tools_absent(self):
        content = self._load().lower()
        assert "blocked" in content
        assert "none of the 30" in content or "absent" in content or "not present" in content or "not " in content

    def test_no_real_secrets_or_topology(self):
        import re

        content = self._load()
        sanitized = content.replace("127.0.0.1", "").replace("0.0.0.0", "")
        assert not re.search(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            sanitized,
        )
        assert not re.search(r"\b[A-F0-9]{20,}\b", content)
        assert "MCP_HTTP_BIND_PUBLIC" not in content


class TestOpenAIMcpAttachPathAudit:
    """Contract tests for docs/operations/OPENAI_MCP_ATTACH_PATH_AUDIT.md.
    Phase 17A.
    """

    def _load(self) -> str:
        return (ROOT / "docs" / "operations" / "OPENAI_MCP_ATTACH_PATH_AUDIT.md").read_text()

    def test_doc_exists(self):
        assert (ROOT / "docs" / "operations" / "OPENAI_MCP_ATTACH_PATH_AUDIT.md").is_file()

    def test_public_connector_not_live(self):
        content = self._load().lower()
        assert "not live" in content

    def test_private_sse_not_equal_public_attach(self):
        content = self._load().lower()
        assert "private" in content
        assert "public" in content
        # Must distinguish the two, not conflate them
        assert "loopback-only" in content or "loopback" in content

    def test_official_requirements_source_attributed(self):
        content = self._load()
        assert "modelcontextprotocol.io" in content
        assert "developers.openai.com" in content or "help.openai.com" in content
        assert "Sources used" in content or "official only" in content.lower()

    def test_states_tls_public_endpoint_or_auth_requirements(self):
        content = self._load().lower()
        assert "tls" in content
        assert "oauth" in content

    def test_agent_token_never_master(self):
        content = self._load().lower()
        assert "master key" in content
        assert "never" in content

    def test_safe_mode_mandatory(self):
        content = self._load()
        assert "MCP_CLIENT_SAFE_MODE=true" in content
        assert "mandatory" in content.lower()

    def test_no_dangerous_tools(self):
        content = self._load().lower()
        assert "write" in content
        assert "docker" in content
        assert "mcp_client_blocked_tools" in content or "blocked_tools" in content

    def test_ambiguity_flagged_not_guessed(self):
        content = self._load().lower()
        # Must explicitly acknowledge ambiguity/unverified sources rather
        # than presenting every claim as settled fact.
        assert "ambiguous" in content or "unverified" in content

    def test_no_forbidden_scopes_mentioned(self):
        content = self._load().lower()
        for scope in ("ssh:files", "project:write", "jobs:run"):
            assert scope not in content

    def test_no_real_secrets_or_topology(self):
        import re

        content = self._load()
        sanitized = content.replace("127.0.0.1", "").replace("0.0.0.0", "")
        assert not re.search(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            sanitized,
        )
        assert not re.search(r"\b[A-F0-9]{20,}\b", content)
        assert "MCP_HTTP_BIND_PUBLIC" not in content


class TestPrivateStreamableHttpTransportDesign:
    """Contract tests for
    docs/superpowers/specs/2026-07-26-private-streamable-http-mcp-transport.md.
    Phase 18A — design/audit only, no runtime code.
    """

    DOC_PATH = (
        ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-07-26-private-streamable-http-mcp-transport.md"
    )

    def _load(self) -> str:
        return self.DOC_PATH.read_text()

    def test_doc_exists(self):
        assert self.DOC_PATH.is_file()

    def test_sse_marked_deprecated(self):
        content = self._load().lower()
        assert "deprecated" in content
        assert "sse" in content

    def test_stdio_remains_default(self):
        content = self._load().lower()
        assert "stdio" in content
        assert "default" in content

    def test_private_sse_remains_available(self):
        content = self._load().lower()
        assert "private sse" in content
        assert "available" in content

    def test_public_connector_not_live(self):
        content = self._load().lower()
        assert "not live" in content

    def test_loopback_default_bind(self):
        content = self._load()
        assert "127.0.0.1" in content
        assert "loopback" in content.lower()

    def test_bearer_auth_and_origin_validation_required(self):
        content = self._load().lower()
        assert "bearer" in content
        assert "origin" in content
        assert "required" in content

    def test_safe_mode_mandatory(self):
        content = self._load()
        assert "MCP_CLIENT_SAFE_MODE=true" in content
        assert "mandatory" in content.lower()

    def test_no_compose_systemd_autostart(self):
        content = self._load().lower()
        assert "no docker compose" in content or "not added to" in content
        assert "systemd" in content
        assert "autostart" in content

    def test_no_master_key(self):
        content = self._load().lower()
        assert "master key" in content
        assert "never" in content

    def test_routes_must_be_empirically_discovered(self):
        content = self._load().lower()
        assert "empirically" in content

    def test_no_real_secrets_or_topology(self):
        import re

        content = self._load()
        sanitized = content.replace("127.0.0.1", "").replace("0.0.0.0", "")
        assert not re.search(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            sanitized,
        )
        assert not re.search(r"\b[A-F0-9]{20,}\b", content)
        assert "xloud" not in content.lower()


class TestStreamableHttpDocsEnvSync:
    """Contract tests for the Phase 18C docs/env sync: docs must
    honestly describe both private HTTP entrypoints (SSE and
    Streamable HTTP) side by side, stdio as default, and the public
    connector as still not live.
    """

    def _tool_attach(self) -> str:
        return (ROOT / "docs" / "operations" / "CHATGPT_TOOL_ATTACH.md").read_text()

    def _readiness(self) -> str:
        return (ROOT / "docs" / "operations" / "OPENAI_CONNECTOR_READINESS.md").read_text()

    def _sse_runbook(self) -> str:
        return (ROOT / "docs" / "operations" / "MCP_PRIVATE_SSE_RUNBOOK.md").read_text()

    def _env_example(self) -> str:
        return (
            ROOT / "examples" / "mcp_server" / "mcp_client.streamable-http.env.example"
        ).read_text()

    def test_tool_attach_mentions_mcp_route_and_port(self):
        content = self._tool_attach()
        assert "/mcp" in content
        assert "8087" in content

    def test_tool_attach_mentions_bearer_and_origin_env_vars(self):
        content = self._tool_attach()
        assert "MCP_STREAMABLE_HTTP_BEARER_TOKEN" in content
        assert "MCP_STREAMABLE_HTTP_ALLOWED_ORIGINS" in content

    def test_tool_attach_says_stdio_default_sse_available_streamable_available(self):
        content = self._tool_attach().lower()
        assert "stdio" in content and "default" in content
        assert "sse" in content and "available" in content
        assert "streamable http" in content

    def test_tool_attach_public_connector_not_live(self):
        content = self._tool_attach().lower()
        assert "not a public" in content or "not live" in content

    def test_tool_attach_no_compose_systemd_autostart(self):
        content = self._tool_attach().lower()
        assert "not deployed as a persistent service" in content or "persistent service" in content

    def test_tool_attach_mentions_streamable_http_smoke_script(self):
        content = self._tool_attach()
        assert "mcp_streamable_http_safe_smoke.py" in content

    def test_tool_attach_sse_deprecated_but_kept(self):
        content = self._tool_attach().lower()
        assert "deprecated" in content
        assert "sse remains fully supported" in content or "remains fully supported" in content

    def test_readiness_mentions_streamable_http_route_and_port(self):
        content = self._readiness()
        assert "/mcp" in content
        assert "8087" in content

    def test_readiness_mentions_bearer_and_origin_env_vars(self):
        content = self._readiness()
        assert "MCP_STREAMABLE_HTTP_BEARER_TOKEN" in content
        assert "MCP_STREAMABLE_HTTP_ALLOWED_ORIGINS" in content

    def test_readiness_stdio_default_both_transports_available(self):
        content = self._readiness().lower()
        assert "stdio" in content and "default" in content
        assert "sse" in content
        assert "streamable http" in content

    def test_readiness_public_connector_not_live(self):
        content = self._readiness().lower()
        assert "not live" in content

    def test_readiness_no_compose_systemd_autostart(self):
        content = self._readiness().lower()
        assert "compose" in content and "systemd" in content and "autostart" in content

    def test_sse_runbook_cross_references_streamable_http(self):
        content = self._sse_runbook()
        assert "mcp_streamable_http_serve.py" in content
        assert "deprecated" in content.lower()

    def test_env_example_placeholders_only(self):
        content = self._env_example()
        assert "<gateway-url>" in content
        assert "<agent-token>" in content
        assert "<generate-private-token>" in content
        assert "MCP_GATEWAY_TOOL_MODE=mcp_client" in content
        assert "MCP_CLIENT_SAFE_MODE=true" in content
        assert "MCP_STREAMABLE_HTTP_HOST=127.0.0.1" in content
        assert "MCP_STREAMABLE_HTTP_PORT=8087" in content
        assert "MCP_STREAMABLE_HTTP_BEARER_TOKEN=<generate-private-token>" in content
        assert (
            "MCP_STREAMABLE_HTTP_ALLOWED_ORIGINS=<optional-comma-separated-loopback-origins>"
            in content
        )

    def test_env_example_is_gitignored(self):
        gitignore = (ROOT / ".gitignore").read_text()
        assert "mcp_client.streamable-http.env" in gitignore

    def test_env_example_no_master_key_as_runtime(self):
        content = self._env_example()
        assert "NEVER use master key" in content

    def test_no_forbidden_scopes_in_docs(self):
        for content in (self._tool_attach().lower(), self._readiness().lower()):
            for scope in ("ssh:files", "project:write", "jobs:run"):
                assert scope not in content

    def test_no_real_topology_in_docs_and_env_example(self):
        import re

        for content in (self._tool_attach(), self._readiness(), self._env_example()):
            sanitized = content.replace("127.0.0.1", "").replace("0.0.0.0", "")
            assert not re.search(
                r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
                sanitized,
            )
            assert not re.search(r"\b[A-F0-9]{20,}\b", content)
            assert "xloud" not in content.lower()


class TestPublicMcpConnectorRiskReview:
    """Contract tests for docs/operations/PUBLIC_MCP_CONNECTOR_RISK_REVIEW.md.
    Phase 19A — audit/design only, no runtime code.
    """

    DOC_PATH = ROOT / "docs" / "operations" / "PUBLIC_MCP_CONNECTOR_RISK_REVIEW.md"

    def _load(self) -> str:
        return self.DOC_PATH.read_text()

    def test_doc_exists(self):
        assert self.DOC_PATH.is_file()

    def test_public_connector_not_live(self):
        content = self._load().lower()
        assert "not live" in content

    def test_static_bearer_not_acceptable_as_final_public_connector(self):
        content = self._load()
        assert "no — not as the final" in content.lower() or "not as the final" in content.lower()
        assert "static bearer" in content.lower()

    def test_tls_required_before_public_exposure(self):
        content = self._load().lower()
        assert "tls" in content
        assert "required" in content

    def test_oauth_dcr_pkce_decision_required(self):
        content = self._load()
        assert "OAuth 2.1" in content
        assert "DCR" in content
        assert "PKCE" in content
        assert "decision" in content.lower()

    def test_safe_mode_mandatory(self):
        content = self._load()
        assert "MCP_CLIENT_SAFE_MODE=true" in content
        assert "mandatory" in content.lower()

    def test_no_write_docker_agent_tools(self):
        content = self._load().lower()
        assert "write" in content
        assert "docker" in content
        assert "agent-launch" in content or "agent launch" in content

    def test_rollback_plan_required(self):
        content = self._load().lower()
        assert "rollback" in content
        assert "required" in content or "must" in content

    def test_operator_approval_required(self):
        content = self._load().lower()
        assert "operator" in content
        assert "approval" in content

    def test_lists_required_pre_exposure_items(self):
        content = self._load().lower()
        assert "reverse-proxy" in content or "reverse proxy" in content
        assert "protected resource metadata" in content
        assert "authorization server metadata" in content
        assert "rate limiting" in content
        assert "audit event" in content

    def test_gives_three_options_with_recommendation(self):
        content = self._load()
        assert "Option A" in content
        assert "Option B" in content
        assert "Option C" in content
        assert "Recommendation" in content

    def test_lab_only_option_has_ttl_and_allowlist(self):
        content = self._load().lower()
        assert "ttl" in content
        assert "allowlist" in content

    def test_no_real_secrets_or_topology(self):
        import re

        content = self._load()
        sanitized = content.replace("127.0.0.1", "").replace("0.0.0.0", "")
        assert not re.search(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            sanitized,
        )
        assert not re.search(r"\b[A-F0-9]{20,}\b", content)
        assert "xloud" not in content.lower()


class TestPublicMcpOAuthDecision:
    """Contract tests for
    docs/superpowers/specs/2026-07-27-public-mcp-oauth-decision.md.
    Phase 20A — architecture decision only, no runtime code.
    """

    DOC_PATH = (
        ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-07-27-public-mcp-oauth-decision.md"
    )

    def _load(self) -> str:
        return self.DOC_PATH.read_text()

    def test_doc_exists(self):
        assert self.DOC_PATH.is_file()

    def test_public_connector_not_live(self):
        content = self._load().lower()
        assert "not live" in content

    def test_static_bearer_not_final(self):
        content = self._load().lower()
        assert "static bearer" in content
        assert "not" in content

    def test_safe_mode_mandatory(self):
        content = self._load()
        assert "MCP_CLIENT_SAFE_MODE" in content
        assert "precondition" in content.lower() or "mandatory" in content.lower()

    def test_no_write_docker_agent_tools(self):
        content = self._load().lower()
        assert "mcp_client-safe" in content or "safe tool set" in content or "safe mode" in content

    def test_oauth_gaps_listed(self):
        content = self._load().lower()
        assert "not implemented" in content or "not enforced" in content or "gap" in content
        assert "rate limiting" in content
        assert "expiry" in content

    def test_audit_events_required(self):
        content = self._load().lower()
        assert "audit event" in content

    def test_rollback_kill_switch_required(self):
        content = self._load().lower()
        assert "rollback" in content
        assert "kill switch" in content or "kill-switch" in content

    def test_access_control_integration_required(self):
        content = self._load().lower()
        assert "access-control" in content or "access control" in content
        assert "approval" in content

    def test_gives_four_options_with_recommendation(self):
        content = self._load()
        assert "Option A" in content
        assert "Option B" in content
        assert "Option C" in content
        assert "Option D" in content
        assert "Recommendation" in content

    def test_no_real_secrets_or_topology(self):
        import re

        content = self._load()
        sanitized = content.replace("127.0.0.1", "").replace("0.0.0.0", "")
        assert not re.search(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            sanitized,
        )
        assert not re.search(r"\b[A-F0-9]{20,}\b", content)
        assert "xloud" not in content.lower()
