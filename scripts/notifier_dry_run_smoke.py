#!/usr/bin/env python3
"""Dry-run smoke test for the gateway Telegram notifier sidecar.

Usage:
    GATEWAY_NOTIFIER_API_KEY=... python scripts/notifier_dry_run_smoke.py
    python scripts/notifier_dry_run_smoke.py --base http://localhost:8085 --api-key KEY

The smoke polls gateway /health and /api/admin/audit/recent once. Telegram
delivery is forced to dry-run mode, so this script never sends real messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.notifier.config import NotifierSettings
from app.notifier.gateway import GatewayAuditClient
from app.notifier.service import GatewayNotifierService
from app.notifier.telegram import TelegramClient


def _build_settings(*, base_url: str, api_key: str, timeout_seconds: float) -> NotifierSettings:
    return NotifierSettings(
        enabled=True,
        dry_run=True,
        gateway_url=base_url.rstrip("/"),
        gateway_api_key=api_key,
        telegram_token="",
        telegram_chat_ids=("dry-run",),
        timeout_seconds=timeout_seconds,
    )


async def run_smoke(*, base_url: str, api_key: str, timeout_seconds: float) -> dict[str, Any]:
    """Run one dry-run notifier poll and return a metadata-only summary."""
    settings = _build_settings(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    gateway = GatewayAuditClient(
        base_url=settings.gateway_url,
        api_key=settings.gateway_api_key,
        timeout_seconds=settings.timeout_seconds,
    )
    telegram = TelegramClient(
        token="",
        chat_ids=("dry-run",),
        dry_run=True,
        timeout_seconds=settings.timeout_seconds,
    )
    service = GatewayNotifierService(settings=settings, gateway=gateway, telegram=telegram)
    try:
        before = await service.status()
        notifications_attempted = await service.poll_once()
        after = await service.status()
        return {
            "ok": True,
            "telegram_delivery": "dry_run",
            "gateway_status_before": before["gateway_health"].get("status"),
            "gateway_status_after": after["gateway_health"].get("status"),
            "notifications_attempted": notifications_attempted,
            "events_notified_total": after["events_notified_total"],
            "prev_health": after["prev_health"],
        }
    finally:
        await service.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run smoke test for gateway notifier")
    parser.add_argument("--base", default=os.getenv("GATEWAY_NOTIFIER_GATEWAY_URL", "http://localhost:8085"))
    parser.add_argument("--api-key", default=os.getenv("GATEWAY_NOTIFIER_API_KEY", ""))
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    if not args.api_key:
        parser.error("API key required: pass --api-key or set GATEWAY_NOTIFIER_API_KEY")

    try:
        result = asyncio.run(
            run_smoke(
                base_url=args.base,
                api_key=args.api_key,
                timeout_seconds=args.timeout,
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "telegram_delivery": "dry_run",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
