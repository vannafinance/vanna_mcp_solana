"""Every tool's reads share repeats and connections: identical in-flight RPC reads send one request, a few safe
answers are reused briefly, and outbound HTTP keeps its connections."""

from __future__ import annotations

import asyncio

import httpx
from solders.pubkey import Pubkey

from vanna_core import client as client_mod
from vanna_core.client import _ResilientRpc
from vanna_core.http_pool import pooled_http

CLOCK = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")
OTHER = Pubkey.from_string("D1x5NnYFZAxhWnYjKvYd3W36LxKdMhPnAdibk3PrUfpz")


class FakeRpc:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_account_info(self, address, **_k):
        self.calls.append(f"account:{address}")
        await asyncio.sleep(0.01)
        return f"data:{address}:{len(self.calls)}"

    async def get_latest_blockhash(self):
        self.calls.append("blockhash")
        await asyncio.sleep(0.01)
        return f"hash:{len(self.calls)}"

    async def send_raw_transaction(self, tx):
        self.calls.append("send")
        return "sig"


async def test_identical_reads_in_flight_send_one_request() -> None:
    fake = FakeRpc()
    rpc = _ResilientRpc(fake)
    a, b, c = await asyncio.gather(rpc.get_account_info(OTHER), rpc.get_account_info(OTHER), rpc.get_account_info(CLOCK))
    assert a == b and fake.calls.count(f"account:{OTHER}") == 1 and fake.calls.count(f"account:{CLOCK}") == 1
    # Account data is never kept once it arrived: the next read asks the chain again.
    await rpc.get_account_info(OTHER)
    assert fake.calls.count(f"account:{OTHER}") == 2


async def test_only_safe_answers_are_reused_and_never_a_send(monkeypatch) -> None:
    fake = FakeRpc()
    rpc = _ResilientRpc(fake)
    first = await rpc.get_latest_blockhash()
    assert await rpc.get_latest_blockhash() == first and fake.calls.count("blockhash") == 1
    clock = await rpc.get_account_info(CLOCK)
    assert await rpc.get_account_info(CLOCK) == clock
    monkeypatch.setattr(client_mod, "_BLOCKHASH_REUSE_S", 0.0)
    rpc2 = _ResilientRpc(fake)
    await rpc2.get_latest_blockhash()
    await rpc2.get_latest_blockhash()
    assert fake.calls.count("blockhash") == 3
    await asyncio.gather(rpc.send_raw_transaction(b"x"), rpc.send_raw_transaction(b"x"))
    assert fake.calls.count("send") == 2


async def test_a_caller_giving_up_does_not_cancel_the_read_for_the_others() -> None:
    fake = FakeRpc()
    rpc = _ResilientRpc(fake)
    impatient = asyncio.ensure_future(rpc.get_account_info(OTHER))
    patient = asyncio.ensure_future(rpc.get_account_info(OTHER))
    await asyncio.sleep(0)
    impatient.cancel()
    assert (await patient).startswith("data:")


async def test_outbound_http_reuses_one_client_with_each_callers_timeout() -> None:
    seen: list[object] = []

    async with pooled_http(5.0) as one, pooled_http(9.0) as two:
        assert one._client is two._client  # type: ignore[attr-defined]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    try:
        httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)  # type: ignore[misc]
        async with pooled_http(7.0) as http:
            assert (await http.get("https://example.test/x")).json() == {"ok": True}
    finally:
        httpx.AsyncClient = real  # type: ignore[misc]
    assert seen and seen[0]["read"] == 7.0  # type: ignore[index]
