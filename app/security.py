"""Security middleware and utilities for SSH Gateway."""

import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

from cryptography.fernet import Fernet
from fastapi import Request, HTTPException
from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

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
    
    Raises ValueError if command is dangerous.
    """
    command_lower = command.lower().strip()
    
    # Check exact matches
    for dangerous in DANGEROUS_COMMANDS:
        if dangerous.lower() in command_lower:
            logger.warning("Blocked dangerous command: %s", command)
            raise ValueError(f"Command contains dangerous pattern: {dangerous}")
    
    # Check regex patterns
    import re
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command_lower):
            logger.warning("Blocked command matching pattern %s: %s", pattern, command)
            raise ValueError(f"Command matches dangerous pattern")
    
    return command


# ---------------------------------------------------------------------------
# Path Validation
# ---------------------------------------------------------------------------

FORBIDDEN_PATHS = {
    '/etc/passwd', '/etc/shadow', '/etc/hosts',
    '/root/.ssh', '/root/.bash_history',
    '/var/log/auth.log', '/var/log/secure',
    '/proc', '/sys', '/dev',
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
    
    # Check for obvious traversal attempts
    if '..' in path or '~' in path:
        logger.warning("Blocked path with traversal: %s", path)
        raise ValueError("Path contains directory traversal characters")
    
    # Check forbidden paths
    for forbidden in FORBIDDEN_PATHS:
        if forbidden in path:
            logger.warning("Blocked forbidden path: %s", path)
            raise ValueError(f"Access to {forbidden} is forbidden")
    
    # If base_path provided, ensure path is within it
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
    
    def __init__(self, master_key: Optional[str] = None):
        """Initialize with master key.
        
        Args:
            master_key: Base64-encoded Fernet key. If None, generates new one.
        """
        if master_key:
            self._fernet = Fernet(master_key.encode())
        else:
            # Generate new key - WARNING: store this securely!
            key = Fernet.generate_key()
            self._fernet = Fernet(key)
            logger.warning("Generated new encryption key: %s", key.decode())
    
    def encrypt(self, data: str) -> str:
        """Encrypt string data."""
        return self._fernet.encrypt(data.encode()).decode()
    
    def decrypt(self, encrypted: str) -> str:
        """Decrypt string data."""
        return self._fernet.decrypt(encrypted.encode()).decode()
    
    def hash_secret(self, data: str) -> str:
        """One-way hash for verification (not reversible)."""
        return hashlib.sha256(data.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Audit Logging
# ---------------------------------------------------------------------------

class AuditLogger:
    """Audit logger for security events."""
    
    def __init__(self, log_file: str = "/app/logs/audit.log"):
        self.logger = logging.getLogger("audit")
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
            session_id, source_ip, command[:100]
        )
    
    def log_file_access(self, session_id: str, path: str, operation: str, source_ip: str):
        """Log file access."""
        self.logger.info(
            "FILE | session=%s | ip=%s | op=%s | path=%s",
            session_id, source_ip, operation, path
        )
    
    def log_auth(self, username: str, success: bool, source_ip: str):
        """Log authentication attempt."""
        self.logger.info(
            "AUTH | user=%s | ip=%s | success=%s",
            username, source_ip, success
        )
    
    def log_security_event(self, event_type: str, details: str, source_ip: str):
        """Log generic security event."""
        self.logger.warning(
            "SECURITY | type=%s | ip=%s | %s",
            event_type, source_ip, details
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
# IP Whitelist/Blacklist
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
# Security Headers
# ---------------------------------------------------------------------------

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
