"""Regression test: a disabled notifier must idle, not exit-and-restart-loop.

Before this fix, app.notifier.__main__._main() returned immediately when
GATEWAY_NOTIFIER_ENABLED was false, which under the sidecar's `restart:
unless-stopped` compose policy produced an infinite, pointless restart
loop every few seconds forever.
"""

from __future__ import annotations

import asyncio

import pytest

from app.notifier.__main__ import _main


@pytest.mark.asyncio
async def test_disabled_notifier_idles_instead_of_returning(monkeypatch):
    monkeypatch.setenv("GATEWAY_NOTIFIER_ENABLED", "false")

    task = asyncio.create_task(_main())
    try:
        # If _main() still returned immediately, the task would already be
        # done well within this window.
        await asyncio.sleep(0.2)
        assert not task.done(), "_main() returned instead of idling forever"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
