"""Configuration for the gateway Telegram notifier sidecar."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(value: str | None, *, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class NotifierSettings:
    """Runtime settings for the notifier.

    The notifier is disabled and dry-run by default. Real Telegram delivery
    requires both `enabled=True` and `dry_run=False`, plus token and chat ids.
    """

    enabled: bool = False
    dry_run: bool = True
    gateway_url: str = "http://localhost:8085"
    gateway_api_key: str = ""
    telegram_token: str = ""
    telegram_chat_ids: tuple[str, ...] = ()
    poll_interval_seconds: float = 5.0
    timeout_seconds: float = 10.0
    event_types: tuple[str, ...] = (
        "command.deny",
        "workspace.readonly_block",
        "session.connect",
        "session.disconnect",
        "system.error",
    )

    @classmethod
    def from_env(cls) -> NotifierSettings:
        """Load settings from environment variables."""
        return cls(
            enabled=_parse_bool(os.getenv("GATEWAY_NOTIFIER_ENABLED"), default=False),
            dry_run=_parse_bool(os.getenv("GATEWAY_NOTIFIER_DRY_RUN"), default=True),
            gateway_url=os.getenv("GATEWAY_NOTIFIER_GATEWAY_URL", "http://localhost:8085").rstrip("/"),
            gateway_api_key=os.getenv("GATEWAY_NOTIFIER_API_KEY", ""),
            telegram_token=os.getenv("GATEWAY_NOTIFIER_TELEGRAM_TOKEN", ""),
            telegram_chat_ids=_parse_csv(os.getenv("GATEWAY_NOTIFIER_CHAT_IDS")),
            poll_interval_seconds=_parse_float(
                os.getenv("GATEWAY_NOTIFIER_POLL_INTERVAL_SECONDS"), default=5.0
            ),
            timeout_seconds=_parse_float(os.getenv("GATEWAY_NOTIFIER_TIMEOUT_SECONDS"), default=10.0),
            event_types=_parse_csv(os.getenv("GATEWAY_NOTIFIER_EVENT_TYPES"))
            or cls.event_types,
        )

    @property
    def can_send_telegram(self) -> bool:
        """Return True only when real Telegram delivery is configured."""
        return bool(
            self.enabled
            and not self.dry_run
            and self.telegram_token
            and self.telegram_chat_ids
        )

    @property
    def can_poll_gateway(self) -> bool:
        """Return True when the notifier can query gateway admin endpoints."""
        return bool(self.enabled and self.gateway_url and self.gateway_api_key)
