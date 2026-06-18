"""Compatibility helpers for redis-py close/aclose differences."""

from __future__ import annotations

import inspect
from typing import Any


async def close_redis_client(redis_client: Any) -> None:
    """Close redis client across redis-py versions.

    redis-py versions differ between close() and aclose().
    Some return awaitables, some close synchronously.
    """
    close = getattr(redis_client, "aclose", None)
    if close is None:
        close = redis_client.close
    result = close()
    if inspect.isawaitable(result):
        await result
