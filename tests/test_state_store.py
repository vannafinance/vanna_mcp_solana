"""The shared state store contract, run against the in-process store and the Firestore store.

The Firestore store is exercised two ways: against an in-memory fake of the Firestore client (always; it proves our
logic: expiry on read, single-use `consume`, exactly one winner of a `put_if_absent` race, counters) and, only when
`FIRESTORE_LIVE_PROJECT` is set and Application Default Credentials work, against the real database in a throwaway
collection that the test deletes. The fake serialises transactions, which is the guarantee Firestore gives; whether
real Firestore retries and isolates the same way is what the live run checks.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

import pytest

from mcp_server.oauth.store import FirestoreRecordStore, MemoryRecordStore, make_store


# ── an in-memory stand-in for google.cloud.firestore.AsyncClient ──────────────────────────────────────────

class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Ref:
    def __init__(self, db, doc_id):
        self._db, self.id = db, doc_id

    async def get(self, transaction=None):
        return _Snap(self._db.docs.get(self.id))

    async def set(self, data):
        self._db.docs[self.id] = dict(data)

    async def delete(self):
        self._db.docs.pop(self.id, None)


class _Txn:
    def __init__(self, db):
        self._db = db

    def set(self, ref, data):
        self._db.docs[ref.id] = dict(data)

    def delete(self, ref):
        self._db.docs.pop(ref.id, None)


class _Collection:
    def __init__(self, db):
        self._db = db

    def document(self, doc_id):
        return _Ref(self._db, doc_id)


class FakeFirestore:
    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    def collection(self, _name):
        return _Collection(self)

    def transaction(self):
        return _Txn(self)


@pytest.fixture
def fake_transactions(monkeypatch):
    """`firestore.async_transactional` -> run the function under the fake's lock (one transaction at a time)."""
    from google.cloud import firestore

    def async_transactional(fn):
        async def run(transaction):
            async with transaction._db.lock:
                return await fn(transaction)

        return run

    monkeypatch.setattr(firestore, "async_transactional", async_transactional)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def _stores(clock):
    return [
        MemoryRecordStore(clock),
        FirestoreRecordStore(client=FakeFirestore(), collection="t", clock=clock),
    ]


@pytest.fixture(params=["memory", "firestore-fake"])
def store_and_clock(request, fake_transactions):
    clock = Clock()
    store = _stores(clock)[0 if request.param == "memory" else 1]
    return store, clock


async def test_put_get_and_expiry(store_and_clock):
    store, clock = store_and_clock
    await store.put("a", {"x": 1, "nested": {"y": [1, 2]}}, 60)
    assert await store.get("a") == {"x": 1, "nested": {"y": [1, 2]}}
    clock.now += 59
    assert await store.get("a") is not None
    clock.now += 2
    assert await store.get("a") is None  # expired: not served even if the backend has not swept it yet


async def test_consume_is_single_use(store_and_clock):
    store, _ = store_and_clock
    await store.put("code", {"u": "w"}, 60)
    assert await store.consume("code") == {"u": "w"}
    assert await store.consume("code") is None
    assert await store.get("code") is None


async def test_concurrent_consume_has_exactly_one_winner(store_and_clock):
    store, _ = store_and_clock
    await store.put("code", {"u": "w"}, 60)
    results = await asyncio.gather(*(store.consume("code") for _ in range(8)))
    assert len([r for r in results if r is not None]) == 1


async def test_consume_of_an_expired_record_returns_none(store_and_clock):
    store, clock = store_and_clock
    await store.put("code", {"u": "w"}, 10)
    clock.now += 11
    assert await store.consume("code") is None


async def test_put_if_absent_one_winner_and_reusable_after_expiry(store_and_clock):
    store, clock = store_and_clock
    wins = await asyncio.gather(*(store.put_if_absent("lock", {"i": i}, 30) for i in range(6)))
    assert sum(wins) == 1
    assert await store.put_if_absent("lock", {"i": 99}, 30) is False
    clock.now += 31
    assert await store.put_if_absent("lock", {"i": 99}, 30) is True  # an expired record does not block


async def test_incr_counts_and_keeps_its_original_expiry(store_and_clock):
    store, clock = store_and_clock
    assert await store.incr("n", 60) == 1
    clock.now += 30
    assert await store.incr("n", 60) == 2
    assert await store.incr("n", 60, by=5) == 7
    clock.now += 31  # 61 s after creation: the window has ended, the counter restarts
    assert await store.incr("n", 60) == 1


async def test_delete_and_slash_in_key(store_and_clock):
    store, _ = store_and_clock
    await store.put("a/b", {"k": 1}, 60)
    assert await store.get("a/b") == {"k": 1}
    await store.delete("a/b")
    assert await store.get("a/b") is None


async def test_concurrent_incr_loses_no_updates(store_and_clock):
    store, _ = store_and_clock
    await asyncio.gather(*(store.incr("hot", 60) for _ in range(20)))
    assert await store.incr("hot", 60, by=0) == 20


def test_make_store_selects_by_setting(monkeypatch):
    from types import SimpleNamespace

    assert isinstance(make_store(SimpleNamespace(state_store="memory", firestore_project="", firestore_collection="c")), MemoryRecordStore)
    with pytest.raises(RuntimeError):
        make_store(SimpleNamespace(state_store="redis", firestore_project="", firestore_collection="c"))


# ── the real database, only when asked for ────────────────────────────────────────────────────────────────

LIVE_PROJECT = os.environ.get("FIRESTORE_LIVE_PROJECT", "")


@pytest.mark.skipif(not LIVE_PROJECT, reason="set FIRESTORE_LIVE_PROJECT (and run `gcloud auth application-default login`)")
async def test_live_firestore_contract():
    collection = f"mcp_state_test_{uuid.uuid4().hex[:10]}"
    store = FirestoreRecordStore(LIVE_PROJECT, collection)
    keys = ["code", "lock", "n", "a/b"]
    try:
        await store.put("code", {"u": "w"}, 60)
        assert await store.get("code") == {"u": "w"}
        results = await asyncio.gather(*(store.consume("code") for _ in range(6)))
        assert len([r for r in results if r is not None]) == 1
        wins = await asyncio.gather(*(store.put_if_absent("lock", {"i": i}, 30) for i in range(6)))
        assert sum(wins) == 1
        await asyncio.gather(*(store.incr("n", 60) for _ in range(10)))
        assert await store.incr("n", 60, by=0) == 10
        await store.put("a/b", {"k": 1}, 60)
        assert await store.get("a/b") == {"k": 1}
        await store.put("short", {"k": 1}, 1)
        await asyncio.sleep(1.5)
        assert await store.get("short") is None
        keys.append("short")
    finally:
        for key in keys:
            await store.delete(key)


def test_public_mode_on_cloud_run_refuses_in_memory_state(monkeypatch):
    from mcp_server import main as m

    monkeypatch.setenv("K_SERVICE", "vanna-mcp-solana-public")
    monkeypatch.setattr(m.settings, "auth_enabled", True)
    monkeypatch.setattr(m.settings, "first_party_client_ids", "")
    monkeypatch.setattr(m.settings, "enable_fork_tools", False)
    monkeypatch.setattr(m.settings, "state_store", "memory")
    with pytest.raises(RuntimeError, match="STATE_STORE=firestore"):
        m.build_public_app()


async def test_a_flood_from_one_address_is_stopped_before_the_shared_store():
    from mcp_server.oauth.http import OAuthRateLimitMiddleware

    class CountingStore(MemoryRecordStore):
        incrs = 0

        async def incr(self, key, ttl_seconds, by=1):
            CountingStore.incrs += 1
            return await super().incr(key, ttl_seconds, by)

    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = OAuthRateLimitMiddleware(ok_app, CountingStore(), register_per_hour=3)
    statuses = []

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "POST", "path": "/register", "headers": [], "client": ("203.0.113.9", 1234)}
    for _ in range(50):
        await mw(scope, receive, send)
    assert statuses.count(200) == 3 and statuses.count(429) == 47
    assert CountingStore.incrs == 3  # only the allowed requests ever touched the shared store
