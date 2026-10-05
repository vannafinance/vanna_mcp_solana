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


# ── shared store (Firestore) ──────────────────────────────────────────────────────────────────────────────

class FirestoreRecordStore:
    """The multi-instance store: every instance of the public service reads and writes the same documents.

    One document per key in a single collection, `{json: <record as JSON text>, expires_at: <datetime>}`. The
    expiry is enforced here on every read (never trusted to Firestore's TTL sweep, which can lag by a day),
    and `expires_at` is also the field a Firestore TTL policy deletes by, so dead records are cleaned up.
    Every read-modify-write (`consume`, `put_if_absent`, `incr`) runs in a Firestore transaction, so two
    instances racing on one authorization code or refresh token still see exactly one winner.
    """

    def __init__(self, project: str | None = None, collection: str = "mcp_state", client: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        if client is None:
            from google.cloud import firestore

            client = firestore.AsyncClient(project=project or None)
        self._db = client
        self._collection = collection
        self._clock = clock

    def _ref(self, key: str):
        # A document id cannot contain "/"; keys are otherwise our own tokens, hashes and base58 addresses.
        return self._db.collection(self._collection).document(key.replace("/", "%2F"))

    def _expiry(self, ttl_seconds: float):
        import datetime as dt

        return dt.datetime.fromtimestamp(self._clock() + ttl_seconds, tz=dt.timezone.utc)

    def _live(self, snap) -> Record | None:
        if snap is None or not snap.exists:
            return None
        data = snap.to_dict() or {}
        expires_at = data.get("expires_at")
        if expires_at is None or expires_at.timestamp() <= self._clock():
            return None
        import json

        return json.loads(data["json"])

    def _doc(self, value: Record, expires_at) -> dict[str, Any]:
        import json

        return {"json": json.dumps(value, separators=(",", ":")), "expires_at": expires_at}

    async def get(self, key: str) -> Record | None:
        return self._live(await self._ref(key).get())

    async def put(self, key: str, value: Record, ttl_seconds: float) -> None:
        await self._ref(key).set(self._doc(value, self._expiry(ttl_seconds)))

    async def put_if_absent(self, key: str, value: Record, ttl_seconds: float) -> bool:
        from google.cloud import firestore

        ref = self._ref(key)
        doc = self._doc(value, self._expiry(ttl_seconds))

        @firestore.async_transactional
        async def txn(transaction) -> bool:
            if self._live(await ref.get(transaction=transaction)) is not None:
                return False
            transaction.set(ref, doc)
            return True

        return await txn(self._db.transaction())

    async def consume(self, key: str) -> Record | None:
        from google.cloud import firestore

        ref = self._ref(key)

        @firestore.async_transactional
        async def txn(transaction) -> Record | None:
            value = self._live(await ref.get(transaction=transaction))
            if value is None:
                return None
            transaction.delete(ref)
            return value

        return await txn(self._db.transaction())

    async def delete(self, key: str) -> None:
        await self._ref(key).delete()

    async def incr(self, key: str, ttl_seconds: float, by: int = 1) -> int:
        from google.cloud import firestore

        ref = self._ref(key)

        @firestore.async_transactional
        async def txn(transaction) -> int:
            snap = await ref.get(transaction=transaction)
            current = self._live(snap)
            if current is not None:
                count = int(current["n"]) + by
                expires_at = snap.to_dict()["expires_at"]  # a counter keeps the expiry it was created with
            else:
                count = by
                expires_at = self._expiry(ttl_seconds)
            transaction.set(ref, self._doc({"n": count}, expires_at))
            return count

        return await txn(self._db.transaction())


def make_store(settings: Any) -> RecordStore:
    """The state store the configuration names: `memory` (one instance) or `firestore` (shared)."""
    kind = (settings.state_store or "memory").strip().lower()
    if kind == "firestore":
        return FirestoreRecordStore(settings.firestore_project or None, settings.firestore_collection)
    if kind != "memory":
        raise RuntimeError(f"STATE_STORE must be 'memory' or 'firestore', not {kind!r}")
    return MemoryRecordStore()
