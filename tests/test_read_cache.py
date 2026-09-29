"""Offline tests for the market-wide read cache (mcp_server/read_cache.py)."""

from __future__ import annotations

import asyncio

import pytest

from mcp_server import read_cache
from mcp_server.read_cache import cached_read

pytestmark = pytest.mark.asyncio


def _counter(result):
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        await asyncio.sleep(0)
        return result() if callable(result) else result

    return calls, factory


async def test_hit_within_ttl_and_copy_per_caller() -> None:
    calls, factory = _counter({"price": 1.0})
    first = await cached_read("k", factory, ttl_seconds=60)
    first["price"] = 999
    second = await cached_read("k", factory, ttl_seconds=60)
    assert second == {"price": 1.0}
    assert calls["n"] == 1


async def test_concurrent_misses_share_one_fetch() -> None:
    calls, factory = _counter({"ok": True})
    results = await asyncio.gather(*(cached_read("k2", factory, ttl_seconds=60) for _ in range(5)))
    assert all(r == {"ok": True} for r in results)
    assert calls["n"] == 1


async def test_errors_are_not_cached() -> None:
    calls, factory = _counter({"error": "internal_error", "message": "rpc down"})
    await cached_read("k3", factory, ttl_seconds=60)
    await cached_read("k3", factory, ttl_seconds=60)
    assert calls["n"] == 2


async def test_expiry_and_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 1000.0}
    monkeypatch.setattr(read_cache.time, "monotonic", lambda: now["t"])
    calls, factory = _counter({"v": 1})
    await cached_read("k4", factory, ttl_seconds=30)
    now["t"] += 31
    await cached_read("k4", factory, ttl_seconds=30)
    assert calls["n"] == 2

    await cached_read("k5", factory, ttl_seconds=0)
    await cached_read("k5", factory, ttl_seconds=0)
    assert calls["n"] == 4
