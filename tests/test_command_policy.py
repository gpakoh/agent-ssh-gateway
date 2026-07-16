"""Tests for command policy engine."""

import pytest

from app.command_policy import (
    contains_dangerous_token,
    contains_shell_redirection,
    evaluate_command_policy,
)


def test_policy_off_allows_dangerous_command():
    decision = evaluate_command_policy(
        "rm -rf /",
        mode="off",
        profile="readonly",
    )

    assert decision.allowed is True
    assert "disabled" in decision.reason.lower()


def test_audit_mode_allows_but_records_would_deny():
    decision = evaluate_command_policy(
        "systemctl restart nginx",
        mode="audit",
        profile="readonly",
    )

    assert decision.allowed is True
    assert "AUDIT_ONLY" in decision.reason
    assert "would_allow=False" in decision.reason


def test_readonly_allows_ls_in_enforce_mode():
    decision = evaluate_command_policy(
        "ls -la /var/log",
        mode="enforce",
        profile="readonly",
    )

    assert decision.allowed is True


def test_readonly_denies_systemctl_in_enforce_mode():
    decision = evaluate_command_policy(
        "systemctl restart nginx",
        mode="enforce",
        profile="readonly",
    )

    assert decision.allowed is False
    assert "readonly" in decision.reason


def test_ops_allows_systemctl_restart():
    decision = evaluate_command_policy(
        "systemctl restart nginx",
        mode="enforce",
        profile="ops",
    )

    assert decision.allowed is True


def test_ops_denies_systemctl_disable():
    decision = evaluate_command_policy(
        "systemctl disable nginx",
        mode="enforce",
        profile="ops",
    )

    assert decision.allowed is False
    assert "disable" in decision.reason


def test_default_denies_reboot():
    decision = evaluate_command_policy(
        "reboot",
        mode="enforce",
        profile="default",
    )

    assert decision.allowed is False


def test_default_allows_safe_command():
    decision = evaluate_command_policy(
        "docker ps",
        mode="enforce",
        profile="default",
    )

    assert decision.allowed is True


def test_sudo_systemctl_root_detection():
    decision = evaluate_command_policy(
        "sudo systemctl restart nginx",
        mode="enforce",
        profile="ops",
    )

    assert decision.allowed is True
    assert decision.command_root == "systemctl"


def test_malformed_command_is_denied_in_enforce():
    decision = evaluate_command_policy(
        "echo 'unterminated",
        mode="enforce",
        profile="default",
    )

    assert decision.allowed is False


def test_echo_redirect_denied_in_enforce():
    """Shell redirect > must be blocked in enforce mode (file-write guardrail)."""
    decision = evaluate_command_policy(
        "echo owned > /tmp/pwned",
        mode="enforce",
        profile="default",
    )

    assert decision.allowed is False
    assert ">" in decision.reason


def test_echo_append_denied_in_enforce():
    """Shell redirect >> must be blocked in enforce mode."""
    decision = evaluate_command_policy(
        "echo more >> /tmp/pwned",
        mode="enforce",
        profile="default",
    )

    assert decision.allowed is False
    assert ">>" in decision.reason


def test_echo_redirect_allowed_in_audit():
    """Audit mode logs but does not block shell redirects."""
    decision = evaluate_command_policy(
        "echo owned > /tmp/pwned",
        mode="audit",
        profile="default",
    )

    assert decision.allowed is True
    assert "AUDIT_ONLY" in decision.reason


def test_default_mode_is_enforce():
    """Default COMMAND_POLICY_MODE must be 'enforce', not 'audit'."""
    from app.config import Settings

    s = Settings()
    assert s.command_policy_mode == "enforce"


# ---------------------------------------------------------------------------
# contains_shell_redirection — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,expected",
    [
        # basic (with spaces)
        ("echo hello > /tmp/f", ">"),
        ("echo hello >> /tmp/f", ">>"),
        # no-space variants
        ("echo x>file", ">"),
        ("echo x> file", ">"),
        ("echo x >file", ">"),
        ("echo x>>file", ">>"),
        ("echo x>> file", ">>"),
        ("echo x >>file", ">>"),
        # fd-prefixed
        ("echo x 1> /tmp/f", "1>"),
        ("echo x 2> /tmp/f", "2>"),
        ("echo x &> /tmp/f", "&>"),
        ("echo x 1>> /tmp/f", "1>>"),
        ("echo x 2>> /tmp/f", "2>>"),
        # clobber
        ("echo x >| /tmp/f", ">|"),
        # input redirection
        ("cat < /etc/passwd", "<"),
        ("cat <<EOF", "<<"),
        # piped output still detected
        ("cat f | tee /tmp/out", None),  # pipe is not a redirect
    ],
)
def test_contains_shell_redirection(command, expected):
    assert contains_shell_redirection(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "echo \"a > b\"",
        "echo 'a > b'",
        "echo \"a >> b\"",
        "echo 'a >> b'",
        "echo \"1> file\"",
        "echo \"a < b\"",
        "echo 'a << EOF'",
    ],
)
def test_contains_shell_redirection_quoted_not_flagged(command):
    """Quoted redirection operators must not be flagged."""
    assert contains_shell_redirection(command) is None


@pytest.mark.parametrize(
    "command,expected",
    [
        # These should be detected even without spaces
        ("echo x>file", ">"),
        ("echo x>>file", ">>"),
        # Quoted = not detected
        ("echo \"a > b\"", None),
    ],
)
def test_contains_dangerous_token_redirect(command, expected):
    """contains_dangerous_token must also catch shell redirections."""
    assert contains_dangerous_token(command) == expected


# ---------------------------------------------------------------------------
# Redirect bypass matrix — end-to-end evaluate_command_policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "echo x>file",
        "echo x >file",
        "echo x> file",
        "echo x > file",
        "echo x>>file",
        "echo x >>file",
        "echo x 2>/dev/null",
        "echo x &>/dev/null",
        "echo x >|/tmp/f",
        "cat < /etc/passwd",
    ],
)
def test_redirect_denied_in_enforce(command):
    """All redirection variants must be denied in enforce mode."""
    decision = evaluate_command_policy(command, mode="enforce", profile="default")
    assert decision.allowed is False


@pytest.mark.parametrize(
    "command",
    [
        "echo x>file",
        "echo x >> /tmp/f",
        "echo x 2>/dev/null",
    ],
)
def test_redirect_allowed_in_audit(command):
    """Audit mode must allow but report the would-be denial."""
    decision = evaluate_command_policy(command, mode="audit", profile="default")
    assert decision.allowed is True
    assert "AUDIT_ONLY" in decision.reason
    assert "would_allow=False" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "echo \"a > b\"",
        "echo 'append >> ok'",
    ],
)
def test_quoted_redirect_allowed_in_enforce(command):
    """Quoted redirections must NOT be denied."""
    decision = evaluate_command_policy(command, mode="enforce", profile="default")
    assert decision.allowed is True
