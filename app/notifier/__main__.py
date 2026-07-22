"""Run the gateway Telegram notifier sidecar."""

from __future__ import annotations

import asyncio
import logging

from app.notifier.config import NotifierSettings
from app.notifier.gateway import GatewayAuditClient
from app.notifier.service import GatewayNotifierService
from app.notifier.telegram import TelegramClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def _main() -> None:
    settings = NotifierSettings.from_env()
    if not settings.enabled:
        logger.info("gateway_notifier_disabled")
        return

    gateway = GatewayAuditClient(
        base_url=settings.gateway_url,
        api_key=settings.gateway_api_key,
        timeout_seconds=settings.timeout_seconds,
    )
    telegram = TelegramClient(
        token=settings.telegram_token,
        chat_ids=settings.telegram_chat_ids,
        dry_run=settings.dry_run,
        timeout_seconds=settings.timeout_seconds,
    )
    service = GatewayNotifierService(settings=settings, gateway=gateway, telegram=telegram)
    await service.run_forever()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
