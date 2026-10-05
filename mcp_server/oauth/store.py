"""Short-lived record store behind the public connector's OAuth state.

Everything the authorization server must remember between requests — registered clients, pending
authorizations, SIWS nonces, authorization codes, refresh tokens, revocations, rate-limit counters —
goes through this one interface, so a multi-instance deployment can swap the in-memory store for a
shared one (Firestore) without touching the provider. Secrets (codes, refresh tokens) are only ever
stored under a SHA-256 of the value, never in the clear.

The contract that matters for security is `consume`: an atomic get-and-delete. An authorization code,
a SIWS nonce and a refresh token are each single-use, and two concurrent requests presenting the same
value must see exactly one winner.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Callable, Protocol

Record = dict[str, Any]


def hash_secret(value: str) -> str:
    """The storage key for a bearer secret (authorization code, refresh token, nonce)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RecordStore(Protocol):
    async def get(self, key: str) -> Record | None: ...

    async def put(self, key: str, value: Record, ttl_seconds: float) -> None: ...

    async def put_if_absent(self, key: str, value: Record, ttl_seconds: float) -> bool: ...

    async def consume(self, key: str) -> Record | None:
        """Atomically return the record and delete it; None if absent or expired."""
        ...

    async def delete(self, key: str) -> None: ...

    async def incr(self, key: str, ttl_seconds: float, by: int = 1) -> int:
        """Add `by` to a counter (created with the given TTL) and return the new value."""
        ...


class MemoryRecordStore:
    """Process-local store: tests, local development, and a single-instance deployment."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._data: dict[str, tuple[float, Record]] = {}
        self._lock = asyncio.Lock()

    def _live(self, key: str) -> Record | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= self._clock():
            del self._data[key]
            return None
        return value

    async def get(self, key: str) -> Record | None:
        async with self._lock:
            value = self._live(key)
            return dict(value) if value is not None else None

    async def put(self, key: str, value: Record, ttl_seconds: float) -> None:
        async with self._lock:
            self._data[key] = (self._clock() + ttl_seconds, dict(value))
            self._sweep()

    async def put_if_absent(self, key: str, value: Record, ttl_seconds: float) -> bool:
        async with self._lock:
            if self._live(key) is not None:
                return False
            self._data[key] = (self._clock() + ttl_seconds, dict(value))
            return True

    async def consume(self, key: str) -> Record | None:
        async with self._lock:
            value = self._live(key)
            if value is None:
                return None
            del self._data[key]
            return dict(value)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def incr(self, key: str, ttl_seconds: float, by: int = 1) -> int:
        async with self._lock:
            current = self._live(key)
            count = (int(current["n"]) if current else 0) + by
            expires_at = self._data[key][0] if current else self._clock() + ttl_seconds
            self._data[key] = (expires_at, {"n": count})
            return count

    def _sweep(self) -> None:
        """Drop expired entries so an unauthenticated flood can't grow the process without bound."""
        if len(self._data) < 2048:
            return
        now = self._clock()
        for key in [k for k, (exp, _) in self._data.items() if exp <= now]:
            del self._data[key]
