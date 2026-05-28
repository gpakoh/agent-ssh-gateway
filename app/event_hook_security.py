"""Security utilities for event hooks — SSRF, HMAC, log masking."""

from __future__ import annotations

import hmac
import hashlib
import ipaddress
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SENSITIVE_HEADERS = frozenset({
    "authorization", "x-api-key", "cookie", "set-cookie", "x-webhook-signature",
})

BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("169.254.169.254/32"),
]


@dataclass
class UrlValidationResult:
    valid: bool
    reason: str = ""


def validate_webhook_url(url: str, allow_http: bool = False) -> UrlValidationResult:
    if not url:
        return UrlValidationResult(False, "URL is empty")

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return UrlValidationResult(False, f"URL parse error: {exc}")

    if parsed.scheme == "http" and not allow_http:
        return UrlValidationResult(False, "HTTP not allowed, use HTTPS")
    if parsed.scheme not in ("http", "https"):
        return UrlValidationResult(False, f"Scheme not allowed: {parsed.scheme}")

    try:
        host = parsed.hostname
        if host is None:
            return UrlValidationResult(False, "No hostname in URL")
        addr = ipaddress.ip_address(host)
        for net in BLOCKED_NETWORKS:
            if addr in net:
                return UrlValidationResult(False, f"Blocked IP range: {net}")
    except ValueError:
        pass

    return UrlValidationResult(True)


def validate_destination_ip(host: str) -> UrlValidationResult:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return UrlValidationResult(False, f"Not a valid IP: {host}")

    for net in BLOCKED_NETWORKS:
        if addr in net:
            return UrlValidationResult(False, f"Blocked IP range: {net}")
    return UrlValidationResult(True)


def sign_payload(secret: str | None, payload: bytes, timestamp: str) -> str | None:
    if not secret:
        return None
    msg = f"{timestamp}.{payload.decode('utf-8', errors='replace')}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def mask_sensitive_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {
        k: "****" if k.lower() in SENSITIVE_HEADERS else v
        for k, v in headers.items()
    }
