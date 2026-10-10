"""Basis vault deposits: the market-session check, and how the Jupiter client reaches Jupiter (the app's proxy, as the
app's own vault actions do, with one retry on a rate limit). Offline."""

from __future__ import annotations

import struct
from types import SimpleNamespace

import httpx
import pytest

from vanna_core.contracts import jupiter as jupiter_mod
from vanna_core.contracts.jupiter import JupiterClient
from vanna_core.contracts.vaults import MARKET_SESSION_MESSAGE, market_after_hours
from vanna_core.phoenix import decode_perp_asset_map

ENTRY = 1584


def _asset_map(*markets: tuple[str, int, bool]) -> bytes:
    """A perp asset map: 48-byte header (count at 32), then one 1584-byte entry per (symbol, asset_id, after_hours)."""
    data = bytearray(48 + ENTRY * len(markets))
    struct.pack_into("<I", data, 32, len(markets))
    for i, (symbol, asset_id, after_hours) in enumerate(markets):
        base = 48 + ENTRY * i
        meta = base + 16
        data[base : base + len(symbol)] = symbol.encode()
        struct.pack_into("<H", data, meta + 944, asset_id & 0xFFFF)
        struct.pack_into("<H", data, meta + 948, asset_id >> 16)
        data[base + 1472] = 0b010 if after_hours else 0
    return bytes(data)


def test_decodes_the_after_hours_bits_like_the_app():
    markets = decode_perp_asset_map(_asset_map(("SOL", 0, False), ("META", 7, True)))
    assert [(m.symbol, m.asset_id, m.after_hours) for m in markets] == [("SOL", 0, False), ("META", 7, True)]


async def test_market_after_hours_reads_the_vaults_market():
    data = _asset_map(("TSLA", 6, False), ("META", 7, True))
    ex = SimpleNamespace(perp_asset_map="MAP")

    class Client:
        async def get_multiple_accounts_raw(self, keys):
            return {"MAP": ("owner", data)}

    assert await market_after_hours(Client(), ex, 7) is True
    assert await market_after_hours(Client(), ex, 6) is False
    assert await market_after_hours(Client(), ex, 99) is None
    assert "outside its trading session" in MARKET_SESSION_MESSAGE


def _settings(**kw):
    base = dict(jupiter_api_key="", jupiter_proxy_url="", perps_helper_url="", price_keeper_url="https://app.example",
                is_fork=True, jupiter_fork_dexes="Whirlpool")
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize(
    "kw, url",
    [
        ({}, "https://app.example/api/jupiter/quote"),  # fork, no key: the app's proxy
        ({"perps_helper_url": "https://helper.example/"}, "https://helper.example/api/jupiter/quote"),
        ({"jupiter_proxy_url": "https://proxy.example/jup/"}, "https://proxy.example/jup/quote"),
        ({"jupiter_api_key": "k"}, "https://api.jup.ag/swap/v1/quote"),  # a key of our own: keyed endpoint
        ({"is_fork": False}, "https://lite-api.jup.ag/swap/v1/quote"),  # off a fork, no key: keyless
    ],
)
def test_jupiter_goes_through_the_apps_proxy_without_a_key(kw, url):
    assert JupiterClient(_settings(**kw))._url("quote") == url


async def test_a_rate_limited_quote_is_retried_once(monkeypatch):
    calls: list[str] = []
    answers = [httpx.Response(429, text="Too Many Requests"), httpx.Response(200, json={"inAmount": "1", "outAmount": "2"})]

    async def request(self, method, url, **kwargs):
        calls.append(url)
        return answers[len(calls) - 1]

    async def no_wait(_secs):
        return None

    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    monkeypatch.setattr(jupiter_mod.asyncio, "sleep", no_wait)
    quote = await JupiterClient(_settings()).get_quote("A", "B", 100)
    assert quote["outAmount"] == "2" and len(calls) == 2


async def test_a_second_rate_limit_is_reported(monkeypatch):
    async def request(self, method, url, **kwargs):
        return httpx.Response(429, text="Too Many Requests")

    async def no_wait(_secs):
        return None

    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    monkeypatch.setattr(jupiter_mod.asyncio, "sleep", no_wait)
    with pytest.raises(Exception, match="HTTP 429"):
        await JupiterClient(_settings()).get_quote("A", "B", 100)
