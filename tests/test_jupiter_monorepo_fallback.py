"""Tests for monorepo /api/solana/* fallback and caching in Jupiter and keeper clients."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from vanna_core.contracts import jupiter as jupiter_mod
from vanna_core.contracts import oracle_refresh as oracle_mod
from vanna_core.contracts import perps as perps_mod


async def test_jupiter_client_falls_back_to_solana_prefix_and_caches(monkeypatch) -> None:
    jupiter_mod._JUPITER_PROXY_PREFIXES.clear()
    seen_urls: list[str] = []

    async def fake_app_request(_http, _settings, _method: str, url: str, **_kwargs):
        seen_urls.append(url)
        if "/api/solana/jupiter/" in url:
            return httpx.Response(200, json={"outAmount": "755000000"})
        return httpx.Response(404, json={"error": "Not found"})

    monkeypatch.setattr(jupiter_mod, "app_request", fake_app_request)
    settings = SimpleNamespace(
        jupiter_api_key="",
        jupiter_proxy_url="",
        perps_helper_url="http://127.0.0.1:3001",
        price_keeper_url="",
        is_fork=True,
    )
    client = jupiter_mod.JupiterClient(settings)

    resp1 = await client._send("GET", "quote", params={"amount": "1"})
    resp2 = await client._send("GET", "swap-instructions")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert seen_urls == [
        "http://127.0.0.1:3001/api/jupiter/quote",
        "http://127.0.0.1:3001/api/solana/jupiter/quote",
        "http://127.0.0.1:3001/api/solana/jupiter/swap-instructions",
    ]


async def test_oracle_refresh_falls_back_to_solana_prefix_and_caches(monkeypatch) -> None:
    if hasattr(oracle_mod.refresh_via_keeper, "_paths"):
        delattr(oracle_mod.refresh_via_keeper, "_paths")
    oracle_mod._keeper_last_refresh = 0.0
    seen_urls: list[str] = []

    async def fake_app_request(_http, _settings, _method: str, url: str, **_kwargs):
        seen_urls.append(url)
        if url.endswith("/api/solana/prices/refresh"):
            return httpx.Response(200, json={"ok": True, "prices": []})
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(oracle_mod, "app_request", fake_app_request)
    settings = SimpleNamespace(
        is_fork=True,
        price_keeper_url="http://127.0.0.1:3001",
        price_keeper_secret="",
    )

    await oracle_mod.refresh_via_keeper(settings)
    oracle_mod._keeper_last_refresh = 0.0
    await oracle_mod.refresh_via_keeper(settings)

    assert seen_urls == [
        "http://127.0.0.1:3001/api/prices/refresh",
        "http://127.0.0.1:3001/api/solana/prices/refresh",
        "http://127.0.0.1:3001/api/solana/prices/refresh",
    ]


async def test_perps_helper_caches_solana_prefix(monkeypatch) -> None:
    perps_mod._HELPER_PREFIXES.clear()
    seen_urls: list[str] = []

    async def fake_app_request(_http, _settings, _method: str, url: str, **_kwargs):
        seen_urls.append(url)
        if "/api/solana/" in url:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text="not found")

    monkeypatch.setattr("vanna_core.app_gate.app_request", fake_app_request)
    settings = SimpleNamespace(is_fork=True, perps_helper_url="http://127.0.0.1:3001", price_keeper_url="")
    contract = perps_mod.PerpsContract.__new__(perps_mod.PerpsContract)
    contract._client = SimpleNamespace(settings=settings)

    assert await contract._helper("/api/perps/phoenix-book", {"market": "SOL-PERP"}) == {"ok": True}
    assert await contract._helper("/api/perps/phoenix-access", {"market": "SOL-PERP"}) == {"ok": True}
    assert seen_urls == [
        "http://127.0.0.1:3001/api/perps/phoenix-book",
        "http://127.0.0.1:3001/api/solana/perps/phoenix-book",
        "http://127.0.0.1:3001/api/solana/perps/phoenix-access",
    ]
