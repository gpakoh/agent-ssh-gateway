"""Security middleware and utilities for SSH Gateway."""

import hashlib
import hmac
import logging
import re
import secrets
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet
from fastapi import Request
from slowapi import Limiter

from app.auth_middleware import get_client_ip, parse_cidrs
from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

def _rate_limit_key(request: Request) -> str:
    """Extract real client IP for rate limiting, respecting trusted proxies."""
    trusted = parse_cidrs(settings.trusted_proxy_cidrs)
    return get_client_ip(request, trusted)


limiter = Limiter(key_func=_rate_limit_key)


def rate_limit_mutation(requests: int = 30, period: str = "minute"):
    """Rate-limit decorator for mutation endpoints.

    Usage:
        @rate_limit_mutation(10, "minute")
        async def my_endpoint(req: SomeRequest, request: Request):
            ...
    """
    return limiter.limit(f"{requests}/{period}")


# ---------------------------------------------------------------------------
# Command Sanitization
# ---------------------------------------------------------------------------

DANGEROUS_COMMANDS = {
    'rm -rf /', 'rm -rf /*', 'rm -rf ~', 'rm -rf /root',
    ':(){ :|:& };:', 'fork bomb', 'dd if=/dev/zero of=/dev/sda',
    'mkfs.ext4 /dev/sda', 'mkfs.ext3 /dev/sda', 'mkfs /dev/sda',
    '> /dev/sda', 'mv / /dev/null',
    'wget http', 'curl http', 'nc -e', 'bash -i',
    'python -c "import socket', 'python3 -c "import socket',
    'perl -e "use Socket"',
    'chmod -R 777 /', 'chmod 777 /',
}

DANGEROUS_PATTERNS = [
    r';\s*rm\s+-rf',
    r'&&\s*rm\s+-rf',
    r'\|\s*bash',
    r'\|\s*sh\s+-c',
    r'curl\s+.*\|\s*bash',
    r'wget\s+.*\|\s*bash',
    r'nc\s+-[lpe]',
    r'mkfifo\s+.*\|\s*bash',
    r'/dev/tcp/',
    r'/dev/udp/',
]


def sanitize_command(command: str) -> str:
    """Sanitize command to prevent dangerous operations.

    WARNING: This is a basic blocklist guardrail, NOT a security boundary.
    It can be bypassed (e.g., encoded payloads, obfuscation).
    Do not rely on this as the sole protection against untrusted users.

    Raises ValueError if command matches a known dangerous pattern.
    """
    command_lower = command.lower().strip()
    
    # Check Exact Matches
    for dangerous in DANGEROUS_COMMANDS:
        if dangerous.lower() in command_lower:
            logger.warning("Blocked dangerous command: %s", command)
            raise ValueError(f"Command contains dangerous pattern: {dangerous}")
    
    # Check Regex Patterns
    import re
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command_lower):
            logger.warning("Blocked command matching pattern %s: %s", pattern, command)
            raise ValueError("Command matches dangerous pattern")
    
    return command


# ---------------------------------------------------------------------------
# Path Validation
# ---------------------------------------------------------------------------

FORBIDDEN_PATHS = {
    '/etc/passwd', '/etc/shadow', '/etc/hosts', '/etc/crontab',
    '/var/spool/cron',
    '/root/.ssh', '/root/.bash_history',
    '/var/log/auth.log', '/var/log/secure',
    '/usr/bin',
    '/proc', '/sys', '/dev', '/boot',
    '..', '../', '/..',
}


def validate_path(path: str, base_path: Optional[str] = None) -> str:
    """Validate file path to prevent directory traversal.
    
    Args:
        path: File path to validate
        base_path: Optional base directory to restrict access to
        
    Returns:
        Validated path
        
    Raises:
        ValueError: If path is forbidden or contains traversal attempts
    """
    import os
    
    path = path.strip()
    
    # Check For Obvious Traversal Attempts
    if '..' in path or '~' in path:
        logger.warning("Blocked path with traversal: %s", path)
        raise ValueError("Path contains directory traversal characters")
    
    # Check Forbidden Paths
    for forbidden in FORBIDDEN_PATHS:
        if forbidden in path:
            logger.warning("Blocked forbidden path: %s", path)
            raise ValueError(f"Access to {forbidden} is forbidden")
    
    # If Base_path Provided, Ensure Path Is Within It
    if base_path:
        abs_path = os.path.abspath(path)
        abs_base = os.path.abspath(base_path)
        if not abs_path.startswith(abs_base):
            logger.warning("Blocked path outside base: %s (base: %s)", path, base_path)
            raise ValueError("Path is outside allowed directory")
    
    return path


# ---------------------------------------------------------------------------
# Secret Encryption
# ---------------------------------------------------------------------------

class SecretManager:
    """Encrypt/decrypt sensitive data like SSH credentials."""
    
    def __init__(self, master_key: str):
        if not master_key:
            raise RuntimeError(
                "ENCRYPTION_KEY is required. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        self._master_key = master_key
        self._fernet = Fernet(master_key.encode())
    
    def encrypt(self, data: str) -> str:
        """Encrypt string data."""
        return self._fernet.encrypt(data.encode()).decode()
    
    def decrypt(self, encrypted: str) -> str:
        """Decrypt string data."""
        return self._fernet.decrypt(encrypted.encode()).decode()
    
    def hash_secret(self, data: str) -> str:
        """One-way hash for verification using HMAC-SHA256."""
        return hmac.new(self._master_key.encode(), data.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Secret Redaction
# ---------------------------------------------------------------------------

SECRET_REDACTION_PLACEHOLDER = "[REDACTED]"

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Specific patterns first (before generic key=value to avoid partial matches)
    (
        re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]+"),
        r"\1" + SECRET_REDACTION_PLACEHOLDER,
    ),
    (
        re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
        r"\1" + SECRET_REDACTION_PLACEHOLDER,
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        SECRET_REDACTION_PLACEHOLDER,
    ),
    # Generic KEY=value / KEY: value (runs last)
    # authorization/bearer omitted here — covered by specific patterns above.
    (
        re.compile(
            r"(?i)\b("
            r"api[_-]?key|token|access[_-]?token|refresh[_-]?token|"
            r"secret|password|passwd|pwd|"
            r"private[_-]?key|client[_-]?secret|webhook[_-]?secret"
            r")\b\s*[:=]\s*([^\s\"']+)"
        ),
        r"\1=" + SECRET_REDACTION_PLACEHOLDER,
    ),
]


def redact_secrets(value: Any) -> Any:
    """Redact obvious secrets from strings, dicts and lists.

    This is a safety net for logs, audit records and event hook payloads.
    It is not a full DLP system.
    """
    if value is None:
        return None

    if isinstance(value, str):
        redacted = value
        for pattern, replacement in _SECRET_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted

    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(
                r"(?i)(api[_-]?key|token|secret|password|passwd|pwd|authorization|private[_-]?key)",
                key_text,
            ):
                result[key] = SECRET_REDACTION_PLACEHOLDER
            else:
                result[key] = redact_secrets(item)
        return result

    if isinstance(value, list):
        return [redact_secrets(item) for item in value]

    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)

    return value


# ---------------------------------------------------------------------------
# Audit Logging
# ---------------------------------------------------------------------------

class AuditLogger:
    """Audit logger for security events."""
    
    def __init__(self, log_file: str = "/app/logs/audit.log"):
        self.logger = logging.getLogger("audit")
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if any(
            isinstance(existing, logging.FileHandler)
            and Path(existing.baseFilename) == log_path.resolve()
            for existing in self.logger.handlers
        ):
            return
        handler = logging.FileHandler(log_file)
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s"
        )
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)
    
    def log_command(self, session_id: str, command: str, source_ip: str):
        """Log command execution."""
        self.logger.info(
            "COMMAND | session=%s | ip=%s | cmd=%s",
            session_id, source_ip, redact_secrets(command)
        )

    def log_file_access(self, session_id: str, path: str, operation: str, source_ip: str):
        """Log file access."""
        self.logger.info(
            "FILE | session=%s | ip=%s | op=%s | path=%s",
            session_id, source_ip, operation, redact_secrets(path)
        )

    def log_auth(self, username: str, success: bool, source_ip: str):
        """Log authentication attempt."""
        self.logger.info(
            "AUTH | user=%s | ip=%s | success=%s",
            redact_secrets(username), source_ip, success
        )

    def log_security_event(self, event_type: str, details: str, source_ip: str):
        """Log generic security event."""
        self.logger.warning(
            "SECURITY | type=%s | ip=%s | %s",
            event_type, source_ip, redact_secrets(details)
        )


# ---------------------------------------------------------------------------
# Session Security
# ---------------------------------------------------------------------------

class SessionSecurity:
    """Session security utilities."""
    
    @staticmethod
    def generate_secure_token(length: int = 32) -> str:
        """Generate cryptographically secure random token."""
        return secrets.token_urlsafe(length)
    
    @staticmethod
    def verify_token(token: str, expected_hash: str, secret: str) -> bool:
        """Verify token using HMAC."""
        computed = hmac.new(
            secret.encode(),
            token.encode(),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, expected_hash)


# ---------------------------------------------------------------------------
# IP Whitelist/blacklist
# ---------------------------------------------------------------------------

class IPFilter:
    """IP address filtering."""
    
    def __init__(self):
        self.whitelist: set = set()
        self.blacklist: set = set()
    
    def add_to_whitelist(self, ip: str):
        """Add IP to whitelist."""
        self.whitelist.add(ip)
    
    def add_to_blacklist(self, ip: str):
        """Add IP to blacklist."""
        self.blacklist.add(ip)
    
    def is_allowed(self, ip: str) -> bool:
        """Check if IP is allowed."""
        if ip in self.blacklist:
            return False
        if self.whitelist and ip not in self.whitelist:
            return False
        return True


# ---------------------------------------------------------------------------
# Target Host Validation (SSRF Protection)
# ---------------------------------------------------------------------------

import ipaddress
import socket


def parse_networks(raw: str) -> list[ipaddress._BaseNetwork]:
    """Parse comma-separated CIDR list into ipaddress networks."""
    networks: list[ipaddress._BaseNetwork] = []

    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        networks.append(ipaddress.ip_network(item, strict=False))

    return networks


def resolve_host_ips(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve hostname/IP into a list of IP addresses."""
    try:
        direct_ip = ipaddress.ip_address(host)
        return [direct_ip]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve target host: {host}") from exc

    ips: list[ipaddress._BaseAddress] = []
    seen: set[str] = set()

    for info in infos:
        ip_raw = info[4][0]
        if ip_raw in seen:
            continue
        seen.add(ip_raw)
        ips.append(ipaddress.ip_address(ip_raw))

    if not ips:
        raise ValueError(f"Could not resolve target host: {host}")

    return ips


def validate_target_host(
    host: str,
    allowed_cidrs: str,
    denied_cidrs: str,
) -> list[str]:
    """Validate that target host resolves only to allowed IP ranges.

    Returns resolved IPs as strings for audit/debug.
    """
    ips = resolve_host_ips(host)
    allowed = parse_networks(allowed_cidrs)
    denied = parse_networks(denied_cidrs)

    for ip in ips:
        if any(ip in network for network in denied):
            raise ValueError(f"Target host {host} resolved to denied IP {ip}")

        if allowed and not any(ip in network for network in allowed):
            raise ValueError(f"Target host {host} resolved to non-allowed IP {ip}")

    return [str(ip) for ip in ips]


# ---------------------------------------------------------------------------
# Security Headers
# ---------------------------------------------------------------------------

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
