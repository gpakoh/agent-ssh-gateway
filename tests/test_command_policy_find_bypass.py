"""Tests for examples/mcp_server/command_policy.py — find's own destructive primaries.

Regression: DENIED_COMMAND_PARTS blocked a few specific `find -exec <target>`
targets (rm/mv/chmod/chown as literal substrings) but never blocked find's
own destructive mechanisms directly — `-delete` needs no exec target at
all, and `-exec <anything else>` runs arbitrary commands find itself never
inspects. `find . -delete` (or `find . -exec cp ... \\;`, `find . -exec tee
... \\;`, etc.) starts with the allowed "find " prefix and matched none of
the denied substrings, despite this module's own docstring calling itself
a "Read-only command policy". Live and reachable: gateway_client.py's
execute_restricted() (the MCP "restricted"/read-only command execution
tool) calls validate_readonly_command() directly.
"""

import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
sys.path.insert(0, str(EXAMPLE_DIR))

from command_policy import CommandPolicyError, validate_readonly_command  # noqa: E402


class TestFindDestructivePrimitivesBlocked:
    def test_find_delete_blocked(self):
        with pytest.raises(CommandPolicyError):
            validate_readonly_command('find . -name "*.py" -delete')

    def test_find_exec_arbitrary_command_blocked(self):
        """-exec runs whatever follows, not just rm/mv/chmod/chown.

        Uses the "+" terminator (no semicolon) so this isolates the -exec
        check itself from the pre-existing ";" denial — find's classic
        "\\;" terminator would already be caught by that unrelated rule.
        """
        with pytest.raises(CommandPolicyError):
            validate_readonly_command("find . -type f -exec cp {} /tmp/exfil +")

    def test_find_execdir_blocked(self):
        with pytest.raises(CommandPolicyError):
            validate_readonly_command("find . -execdir tee {} +")

    def test_find_ok_blocked(self):
        with pytest.raises(CommandPolicyError):
            validate_readonly_command("find . -ok cp {} /tmp/x +")

    def test_find_fprintf_blocked(self):
        with pytest.raises(CommandPolicyError):
            validate_readonly_command("find . -fprintf /etc/cron.d/evil %p\\n")

    def test_plain_find_still_allowed(self):
        result = validate_readonly_command('find . -name "*.py"')
        assert result == 'find . -name "*.py"'
