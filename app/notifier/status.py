"""Gateway status rendering for Telegram notifications.

Renders health dict into Telegram-safe status text.
No host/IP/secrets/raw env values.
"""

from __future__ import annotations

from app.notifier.formatting import render_health_status

__all__ = ["render_health_status"]
