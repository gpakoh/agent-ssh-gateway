"""End-to-end MCP regression tests — catch exactly the breakage Claude found.

Tests verify:
1. project_run_pytest/ruff/mypy commands pass MCP-local allowlist AND testlint profile
2. execute_restricted allowed commands pass both layers
3. execute_restricted dangerous commands blocked at both layers
4. Combined cd && command format (used by execute_project_command) vs metachar gate
5. _run_uv_tool calls execute_raw (NOT execute_project_command) — permanent regression guard
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# MCP server path setup
# ---------------------------------------------------------------------------
_EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
_MCP_SERVER_DIR = _EXAMPLES_DIR / "mcp_server"
if str(_MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_SERVER_DIR))

from command_policy import (  # noqa: E402
    CommandPolicyError,
    validate_readonly_command,
)

from app.command_policy import (  # noqa: E402
    CommandPolicyMode,
    CommandPolicyProfile,
    evaluate_command_policy,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def enforce_testlint():
    """Return kwargs for evaluate_command_policy in enforce+testlint mode."""
    return dict(mode=CommandPolicyMode.ENFORCE.value, profile=CommandPolicyProfile.TESTLINT.value)


@pytest.fixture
def enforce_default():
    """Return kwargs for evaluate_command_policy in enforce+default mode."""
    return dict(mode=CommandPolicyMode.ENFORCE.value, profile=CommandPolicyProfile.DEFAULT.value)


# ---------------------------------------------------------------------------
# Commands that _run_uv_tool generates (from chatgpt_tools.py lines 492-503)
# ---------------------------------------------------------------------------

# The "uv available?" check — executed via execute_raw (no cd wrapping)
UV_CHECK_COMMAND = "command -v uv"

# Actual uv tool commands (joined with shlex.quote in _run_uv_tool)
# _build_uv_argv returns: ["uv", "run", "--frozen", "--directory", dir, "--"] + cmd + ["--"] + targets
# Then " ".join(shlex.quote(a) for a in argv) is used.
# These are the shell-quoted forms:
PYTEST_CMD = "uv run --frozen --directory /tmp/proj -- pytest -- ."
RUFF_CMD = "uv run --frozen --directory /tmp/proj -- ruff check -- ."
MYPY_CMD = "uv run --frozen --directory /tmp/proj -- mypy -- ."

# What execute_project_command actually sends (cd wrapping) — still used by repo_status etc.
PYTEST_FULL = f"cd /tmp/proj && {PYTEST_CMD}"
RUFF_FULL = f"cd /tmp/proj && {RUFF_CMD}"
MYPY_FULL = f"cd /tmp/proj && {MYPY_CMD}"
UV_CHECK_FULL = f"cd /tmp/proj && {UV_CHECK_COMMAND}"


# ===================================================================
# Test 1-3: project_run_pytest / ruff / mypy
# ===================================================================


class TestProjectRunToolsMcpLocalAllowlist:
    """Verify MCP-local allowlist (validate_readonly_command) accepts uv tool commands.

    This is Layer 1: the allowlist in examples/mcp_server/command_policy.py.
    Only applies to execute_restricted — project_run_* tools bypass it via
    execute_project_command.  But the commands must STILL be valid if someone
    routes them through execute_restricted.
    """

    def test_uv_check_passes_allowlist(self):
        """'command -v uv' must be in MCP-local allowlist."""
        result = validate_readonly_command(UV_CHECK_COMMAND)
        assert result == UV_CHECK_COMMAND

    def test_pytest_command_passes_allowlist(self):
        """'uv run ... pytest ...' must start with allowed prefix 'uv '."""
        result = validate_readonly_command(PYTEST_CMD)
        assert result == PYTEST_CMD

    def test_ruff_command_passes_allowlist(self):
        """'uv run ... ruff check ...' must start with allowed prefix 'uv '."""
        result = validate_readonly_command(RUFF_CMD)
        assert result == RUFF_CMD

    def test_mypy_command_passes_allowlist(self):
        """'uv run ... mypy ...' must start with allowed prefix 'uv '."""
        result = validate_readonly_command(MYPY_CMD)
        assert result == MYPY_CMD


class TestProjectRunToolsServerPolicy:
    """Verify server-side command policy (evaluate_command_policy) under testlint profile.

    This is Layer 2: the authoritative policy in app/command_policy.py.
    project_run_* tools go through execute_project_command → /api/ssh/execute,
    which evaluates the command against the server-side profile.
    """

    def test_uv_check_under_testlint(self, enforce_testlint):
        """'command -v uv' passes testlint: command is in TESTLINT_ROOTS, -v is allowed."""
        d = evaluate_command_policy(UV_CHECK_COMMAND, **enforce_testlint)
        assert d.allowed, f"command -v uv blocked under testlint: {d.reason}"

    def test_pytest_under_testlint(self, enforce_testlint):
        """'uv run ... pytest ...' passes testlint: uv is in TESTLINT_ROOTS."""
        d = evaluate_command_policy(PYTEST_CMD, **enforce_testlint)
        assert d.allowed, f"pytest command blocked under testlint: {d.reason}"

    def test_ruff_under_testlint(self, enforce_testlint):
        """'uv run ... ruff check ...' passes testlint."""
        d = evaluate_command_policy(RUFF_CMD, **enforce_testlint)
        assert d.allowed, f"ruff command blocked under testlint: {d.reason}"

    def test_mypy_under_testlint(self, enforce_testlint):
        """'uv run ... mypy ...' passes testlint."""
        d = evaluate_command_policy(MYPY_CMD, **enforce_testlint)
        assert d.allowed, f"mypy command blocked under testlint: {d.reason}"

    def test_uv_check_under_default(self, enforce_default):
        """'command -v uv' also passes default profile (cd not in DENIED_ROOTS)."""
        d = evaluate_command_policy(UV_CHECK_COMMAND, **enforce_default)
        assert d.allowed, f"command -v uv blocked under default: {d.reason}"


class TestProjectRunToolsCombinedCommands:
    """Verify the cd && command format used by execute_project_command.

    execute_project_command wraps commands: f"cd {root}/{proj} && {command}"
    The && is a metachar that hits Gate 1 (blanket metachar denial).

    project_run_pytest/ruff/mypy now use execute_raw() which sends commands
    WITHOUT cd wrapping.  These tests verify:
    1. Raw commands (no &&) pass under testlint — the new working path
    2. cd && commands remain blocked — execute_project_command still uses &&
    """

    def test_cd_and_uv_check_blocked_by_metachar(self, enforce_testlint):
        """'cd /tmp/proj && command -v uv' → && blocked by metachar gate."""
        d = evaluate_command_policy(UV_CHECK_FULL, **enforce_testlint)
        assert not d.allowed
        assert "Metacharacter" in d.reason or "metachar" in d.reason.lower()

    def test_cd_and_pytest_blocked_by_metachar(self, enforce_testlint):
        """'cd /tmp/proj && uv run ... pytest ...' → && blocked."""
        d = evaluate_command_policy(PYTEST_FULL, **enforce_testlint)
        assert not d.allowed

    def test_cd_and_ruff_blocked_by_metachar(self, enforce_testlint):
        """'cd /tmp/proj && uv run ... ruff check ...' → && blocked."""
        d = evaluate_command_policy(RUFF_FULL, **enforce_testlint)
        assert not d.allowed

    def test_cd_and_mypy_blocked_by_metachar(self, enforce_testlint):
        """'cd /tmp/proj && uv run ... mypy ...' → && blocked."""
        d = evaluate_command_policy(MYPY_FULL, **enforce_testlint)
        assert not d.allowed

    def test_cd_only_command_allowed(self, enforce_testlint):
        """'cd /tmp/proj' alone passes — no metachar."""
        d = evaluate_command_policy("cd /tmp/proj", **enforce_testlint)
        assert d.allowed, f"cd alone blocked: {d.reason}"


# ===================================================================
# Test: execute_raw path — commands without cd && wrapping
# ===================================================================


class TestExecuteRawCommands:
    """Verify commands sent via execute_raw() (no cd && wrapping).

    _run_uv_tool now uses execute_raw() instead of execute_project_command().
    These commands must pass BOTH MCP-local allowlist AND server-side testlint.
    """

    def test_uv_check_no_metachar(self):
        """'command -v uv' contains no shell metacharacters."""
        assert "&&" not in UV_CHECK_COMMAND
        assert "|" not in UV_CHECK_COMMAND
        assert ";" not in UV_CHECK_COMMAND

    def test_pytest_no_metachar(self):
        """pytest command contains no shell metacharacters."""
        assert "&&" not in PYTEST_CMD
        assert "|" not in PYTEST_CMD
        assert ";" not in PYTEST_CMD

    def test_ruff_no_metachar(self):
        """ruff command contains no shell metacharacters."""
        assert "&&" not in RUFF_CMD
        assert "|" not in RUFF_CMD
        assert ";" not in RUFF_CMD

    def test_mypy_no_metachar(self):
        """mypy command contains no shell metacharacters."""
        assert "&&" not in MYPY_CMD
        assert "|" not in MYPY_CMD
        assert ";" not in MYPY_CMD

    def test_uv_check_passes_mcp_local_allowlist(self):
        """'command -v uv' passes MCP-local allowlist."""
        result = validate_readonly_command(UV_CHECK_COMMAND)
        assert result == UV_CHECK_COMMAND

    def test_pytest_passes_mcp_local_allowlist(self):
        """'uv run ... pytest ...' passes MCP-local allowlist (uv prefix)."""
        result = validate_readonly_command(PYTEST_CMD)
        assert result == PYTEST_CMD

    def test_ruff_passes_mcp_local_allowlist(self):
        result = validate_readonly_command(RUFF_CMD)
        assert result == RUFF_CMD

    def test_mypy_passes_mcp_local_allowlist(self):
        result = validate_readonly_command(MYPY_CMD)
        assert result == MYPY_CMD

    def test_uv_check_passes_server_testlint(self, enforce_testlint):
        """'command -v uv' passes server-side testlint profile."""
        d = evaluate_command_policy(UV_CHECK_COMMAND, **enforce_testlint)
        assert d.allowed, f"command -v uv blocked under testlint: {d.reason}"

    def test_pytest_passes_server_testlint(self, enforce_testlint):
        """'uv run ... pytest ...' passes server-side testlint."""
        d = evaluate_command_policy(PYTEST_CMD, **enforce_testlint)
        assert d.allowed, f"pytest command blocked under testlint: {d.reason}"

    def test_ruff_passes_server_testlint(self, enforce_testlint):
        d = evaluate_command_policy(RUFF_CMD, **enforce_testlint)
        assert d.allowed, f"ruff command blocked under testlint: {d.reason}"

    def test_mypy_passes_server_testlint(self, enforce_testlint):
        d = evaluate_command_policy(MYPY_CMD, **enforce_testlint)
        assert d.allowed, f"mypy command blocked under testlint: {d.reason}"

    def test_cd_and_pytest_still_blocked(self, enforce_testlint):
        """Raw 'cd x && pytest' remains blocked (metachar gate)."""
        d = evaluate_command_policy(PYTEST_FULL, **enforce_testlint)
        assert not d.allowed
        assert "Metacharacter" in d.reason or "metachar" in d.reason.lower()

    def test_cd_and_ruff_still_blocked(self, enforce_testlint):
        d = evaluate_command_policy(RUFF_FULL, **enforce_testlint)
        assert not d.allowed

    def test_cd_and_mypy_still_blocked(self, enforce_testlint):
        d = evaluate_command_policy(MYPY_FULL, **enforce_testlint)
        assert not d.allowed


# ===================================================================
# Test: _run_uv_tool code path — must call execute_raw, NOT execute_project_command
# ===================================================================


class TestRunUvToolCodePath:
    """Verify _run_uv_tool uses execute_raw (no cd && wrapping).

    This is the permanent regression guard: if someone accidentally reverts
    _run_uv_tool to use execute_project_command, these tests fail because
    execute_project_command emits '&&' which is blocked by the metachar gate.
    """

    def _make_mock_client(self):
        """Create a mock GatewayClient with execute_raw and execute_project_command."""
        client = MagicMock()
        client.execute_raw.return_value = {"job_id": "job-1", "exit_code": 0}
        client.wait_job.return_value = {
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "execution_duration_ms": 100,
            "job_id": "job-1",
        }
        return client

    def test_pytest_calls_execute_raw(self, monkeypatch):
        """project_run_pytest must call execute_raw, NOT execute_project_command."""
        from examples.mcp_server.chatgpt_tools import project_run_pytest

        monkeypatch.setattr(
            "examples.mcp_server.chatgpt_tools._resolve_project",
            lambda _: Path("/tmp/proj"),
        )
        client = self._make_mock_client()
        project_run_pytest(client, project="test-project", target=["."])

        assert client.execute_raw.call_count == 2
        client.execute_project_command.assert_not_called()

    def test_ruff_calls_execute_raw(self, monkeypatch):
        """project_run_ruff must call execute_raw, NOT execute_project_command."""
        from examples.mcp_server.chatgpt_tools import project_run_ruff

        monkeypatch.setattr(
            "examples.mcp_server.chatgpt_tools._resolve_project",
            lambda _: Path("/tmp/proj"),
        )
        client = self._make_mock_client()
        project_run_ruff(client, project="test-project", target=["."])

        assert client.execute_raw.call_count == 2
        client.execute_project_command.assert_not_called()

    def test_mypy_calls_execute_raw(self, monkeypatch):
        """project_run_mypy must call execute_raw, NOT execute_project_command."""
        from examples.mcp_server.chatgpt_tools import project_run_mypy

        monkeypatch.setattr(
            "examples.mcp_server.chatgpt_tools._resolve_project",
            lambda _: Path("/tmp/proj"),
        )
        client = self._make_mock_client()
        project_run_mypy(client, project="test-project", target=["."])

        assert client.execute_raw.call_count == 2
        client.execute_project_command.assert_not_called()

    def test_execute_raw_commands_contain_no_metachar(self, monkeypatch):
        """Every command passed to execute_raw must contain no shell metachars."""
        from examples.mcp_server.chatgpt_tools import _run_uv_tool

        monkeypatch.setattr(
            "examples.mcp_server.chatgpt_tools._resolve_project",
            lambda _: Path("/tmp/proj"),
        )
        client = self._make_mock_client()
        _run_uv_tool(client, project="test-project", tool_key="pytest", tool_name="test", target=["."])

        for call_args in client.execute_raw.call_args_list:
            command = call_args[0][0]
            assert "&&" not in command, f"execute_raw received command with &&: {command!r}"
            assert "|" not in command, f"execute_raw received command with |: {command!r}"
            assert ";" not in command, f"execute_raw received command with ;: {command!r}"


# ===================================================================
# Test 4: execute_restricted allowed commands
# ===================================================================


class TestExecuteRestrictedAllowed:
    """Verify allowed commands pass BOTH MCP-local allowlist AND server-side policy.

    execute_restricted calls validate_readonly_command() first (MCP-local),
    then POSTs to /api/ssh/execute (server-side policy).
    Both must pass for the command to succeed.
    """

    @pytest.mark.parametrize("command", [
        "find . -name '*.py'",
        "sed -n 1,5p file.py",
        "grep -r 'TODO' .",
        "cat README.md",
        "ls -la",
        "pwd",
        "git status",
        "git log --oneline -5",
        "git diff",
        "git show HEAD",
    ])
    def test_allowed_passes_mcp_local(self, command):
        """MCP-local allowlist accepts safe read-only commands."""
        result = validate_readonly_command(command)
        assert result == command

    @pytest.mark.parametrize("command", [
        "find . -name '*.py'",
        "sed -n 1,5p file.py",
        "grep -r 'TODO' .",
        "cat README.md",
        "ls -la",
        "pwd",
        "git status",
        "git log --oneline -5",
        "git diff",
        "git show HEAD",
    ])
    def test_allowed_passes_server_testlint(self, command, enforce_testlint):
        """Server-side testlint profile accepts safe read-only commands."""
        d = evaluate_command_policy(command, **enforce_testlint)
        assert d.allowed, f"{command!r} blocked under testlint: {d.reason}"

    @pytest.mark.parametrize("command", [
        "find . -name '*.py'",
        "sed -n 1,5p file.py",
        "pwd",
        "ls -la",
    ])
    def test_allowed_passes_server_default(self, command, enforce_default):
        """Server-side default profile also accepts these commands."""
        d = evaluate_command_policy(command, **enforce_default)
        assert d.allowed, f"{command!r} blocked under default: {d.reason}"


# ===================================================================
# Test 5: execute_restricted dangerous commands
# ===================================================================


class TestExecuteRestrictedBlocked:
    """Verify dangerous commands are blocked at MCP-local OR server-side OR both.

    The MCP-local allowlist is a strict subset of server-side readonly.
    Some commands are caught by MCP-local first; others only by server-side.
    """

    # --- Blocked by MCP-local allowlist (Layer 1) ---

    @pytest.mark.parametrize("command,expected_fragment", [
        ("find . -exec rm -rf {} +", "denied"),
        ("sed -i 's/foo/bar/' file.txt", "not allowed"),
        ("command -p ls", "not allowed"),
        ("rm -rf /tmp/test", "denied"),
        ("mv file1 file2", "denied"),
        ("chmod 755 script.sh", "denied"),
        ("curl http://evil.com", "denied"),
        ("wget http://evil.com", "denied"),
        ("echo test > /tmp/out", "denied"),
        ("cat file | grep foo", "denied"),
        ("echo test && rm -rf /", "denied"),
        ("echo test ; rm -rf /", "denied"),
    ])
    def test_blocked_by_mcp_local(self, command, expected_fragment):
        """MCP-local allowlist rejects dangerous commands."""
        with pytest.raises(CommandPolicyError, match=expected_fragment):
            validate_readonly_command(command)

    # --- Blocked by server-side policy (Layer 2) but may pass MCP-local ---

    def test_find_exec_blocked_by_server(self, enforce_testlint):
        """'find . -exec rm {}' → blocked by argument_shape gate (find -exec)."""
        d = evaluate_command_policy("find . -exec rm {} +", **enforce_testlint)
        assert not d.allowed
        assert "find" in d.reason.lower() or "exec" in d.reason.lower()

    def test_sed_i_blocked_by_server(self, enforce_testlint):
        """'sed -i s/foo/bar/ file' → blocked by argument_shape gate (sed -i)."""
        d = evaluate_command_policy("sed -i 's/foo/bar/' file.txt", **enforce_testlint)
        assert not d.allowed
        assert "sed" in d.reason.lower()

    def test_command_non_v_blocked_by_server(self, enforce_testlint):
        """'command -p ls' → blocked: only command -v is allowed."""
        d = evaluate_command_policy("command -p ls", **enforce_testlint)
        assert not d.allowed

    def test_rm_blocked_by_server(self, enforce_testlint):
        """'rm -rf /tmp/test' → blocked by DENIED_ROOTS."""
        d = evaluate_command_policy("rm -rf /tmp/test", **enforce_testlint)
        assert not d.allowed

    def test_pipe_blocked_by_server(self, enforce_testlint):
        """'cat file | grep foo' → blocked by metachar gate (|)."""
        d = evaluate_command_policy("cat file | grep foo", **enforce_testlint)
        assert not d.allowed
        assert "Metacharacter" in d.reason

    def test_semicolon_blocked_by_server(self, enforce_testlint):
        """'echo test ; rm -rf /' → blocked by metachar gate (;)."""
        d = evaluate_command_policy("echo test ; rm -rf /", **enforce_testlint)
        assert not d.allowed

    def test_and_blocked_by_server(self, enforce_testlint):
        """'echo test && rm -rf /' → blocked by metachar gate (&&)."""
        d = evaluate_command_policy("echo test && rm -rf /", **enforce_testlint)
        assert not d.allowed

    def test_docker_prune_allowed_by_default(self, enforce_default):
        """'docker system prune -f' → allowed by default profile (only DENIED_ROOTS checked).

        The default profile is permissive — it only blocks root commands in
        DENIED_ROOTS (rm, mv, dd, etc.).  docker is NOT in DENIED_ROOTS,
        so it passes default.  This is expected: default is defense-in-depth,
        not a comprehensive deny list.
        """
        d = evaluate_command_policy("docker system prune -f", **enforce_default)
        assert d.allowed, f"docker system prune blocked by default: {d.reason}"

    def test_docker_prune_blocked_by_readonly(self):
        """'docker system prune -f' → blocked under readonly (docker not in READONLY_ROOTS)."""
        d = evaluate_command_policy(
            "docker system prune -f",
            mode=CommandPolicyMode.ENFORCE.value,
            profile=CommandPolicyProfile.READONLY.value,
        )
        assert not d.allowed
        assert "docker" in d.reason.lower() or "root" in d.reason.lower()

    def test_pip_install_blocked_by_mcp_local(self):
        """'pip install requests' → blocked by MCP-local DENIED_COMMAND_PARTS."""
        with pytest.raises(CommandPolicyError, match="denied"):
            validate_readonly_command("pip install requests")

    # --- Cross-layer consistency ---

    def test_mcp_local_subset_of_server(self, enforce_testlint):
        """Every command blocked by MCP-local should also be blocked by server-side.

        This verifies the MCP-local allowlist is a strict subset of the
        server-side testlint profile (defense-in-depth, no bypass).
        """
        dangerous_commands = [
            "rm -rf /tmp/test",
            "mv file1 file2",
            "chmod 755 script.sh",
            "curl http://evil.com",
            "echo test > /tmp/out",
            "cat file | grep foo",
        ]
        for cmd in dangerous_commands:
            # MCP-local blocks it
            with pytest.raises(CommandPolicyError):
                validate_readonly_command(cmd)
            # Server-side also blocks it
            d = evaluate_command_policy(cmd, **enforce_testlint)
            assert not d.allowed, f"{cmd!r} blocked by MCP-local but allowed by server testlint"


# ===================================================================
# Test 6: No docs update unless behavior is fully implemented
# ===================================================================


class TestDocsConsistency:
    """Verify documentation claims match actual implementation.

    If behavior is documented but not implemented, the test catches the drift.
    """

    def test_testlint_profile_includes_uv(self):
        """TESTLINT_ROOTS must include 'uv' for project_run_* tools to work."""
        from app.command_policy import TESTLINT_ROOTS
        assert "uv" in TESTLINT_ROOTS

    def test_testlint_profile_includes_command(self):
        """TESTLINT_ROOTS must include 'command' for 'command -v uv' check."""
        from app.command_policy import TESTLINT_ROOTS
        assert "command" in TESTLINT_ROOTS

    def test_testlint_profile_includes_pytest_ruff_mypy(self):
        """TESTLINT_ROOTS must include pytest, ruff, mypy."""
        from app.command_policy import TESTLINT_ROOTS
        for tool in ("pytest", "ruff", "mypy"):
            assert tool in TESTLINT_ROOTS, f"{tool} missing from TESTLINT_ROOTS"

    def test_mcp_local_allows_uv_prefix(self):
        """MCP-local allowlist must include 'uv ' prefix."""
        from examples.mcp_server.command_policy import ALLOWED_COMMAND_PREFIXES
        assert "uv " in ALLOWED_COMMAND_PREFIXES

    def test_mcp_local_allows_command_v_uv(self):
        """MCP-local allowlist must include 'command -v uv'."""
        from examples.mcp_server.command_policy import ALLOWED_COMMAND_PREFIXES
        assert "command -v uv" in ALLOWED_COMMAND_PREFIXES

    def test_command_policy_has_testlint_profile(self):
        """CommandPolicyProfile enum must include TESTLINT."""
        assert CommandPolicyProfile.TESTLINT.value == "testlint"

    def test_profile_for_identity_returns_testlint(self):
        """profile_for_identity returns testlint when fingerprint is mapped."""
        from app.command_policy import profile_for_identity
        result = profile_for_identity(
            "abc123def012",
            key_profiles={"abc123def012": "testlint"},
            default_profile="default",
        )
        assert result == "testlint"

    def test_profile_for_identity_falls_back_to_default(self):
        """profile_for_identity falls back to default when fingerprint not mapped."""
        from app.command_policy import profile_for_identity
        result = profile_for_identity(
            "unknownfingerprint",
            key_profiles={"abc123def012": "testlint"},
            default_profile="default",
        )
        assert result == "default"
