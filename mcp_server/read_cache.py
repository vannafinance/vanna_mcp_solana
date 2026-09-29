"""Short-TTL in-process cache for market-wide read responses.

Protocol config, asset registry, prices, pool stats and Kamino markets each
fan out to several RPC reads but are the same for every caller, so repeat
prompts within READ_CACHE_TTL_SECONDS (default 30 s) reuse one result.
Per-wallet reads and every write bypass it. Error results are never cached,
and each caller gets its own copy so a mutation can't leak into the cache.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any, Awaitable, Callable

_locks: dict[str, asyncio.Lock] = {}
_entries: dict[str, tuple[float, Any]] = {}


def _ttl() -> float:
    from vanna_core.config import get_settings

    return float(get_settings().read_cache_ttl_seconds)


def _is_cacheable(value: Any) -> bool:
    return not (isinstance(value, dict) and "error" in value)


async def cached_read(key: str, factory: Callable[[], Awaitable[Any]], ttl_seconds: float | None = None) -> Any:
    """Return a cached value for `key`, or await `factory()` at most once per TTL window."""
    ttl = _ttl() if ttl_seconds is None else ttl_seconds
    if ttl <= 0:
        return await factory()

    hit = _entries.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return copy.deepcopy(hit[1])

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _entries.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return copy.deepcopy(hit[1])
        value = await factory()
        if _is_cacheable(value):
            _entries[key] = (time.monotonic() + ttl, copy.deepcopy(value))
        return value


def clear_read_cache() -> None:
    _entries.clear()
    _locks.clear()
