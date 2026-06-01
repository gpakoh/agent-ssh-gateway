"""Tests for command policy engine."""

from app.command_policy import evaluate_command_policy


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
