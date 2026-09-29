"""Tests for the `dev-fork` oracle adapter (`vanna_core.contracts.oracle_refresh`).

Verifies the byte-for-byte port of `vanna-credit-backend`'s
`buildForkPriceAccountData`/`refreshForkPrices` actually posts a fresh,
valid `PriceUpdateV2` via the `surfnet_setAccount` cheatcode — called
directly against the fork RPC, not through the app's (confirmed
unreachable) `/api/prices/refresh` HTTP route.
"""

from __future__ import annotations

import pytest

from vanna_core.assets import ASSETS
from vanna_core.client import SolanaClient
from vanna_core.config import VannaSettings, get_settings
from vanna_core.contracts.oracle_refresh import DevForkOracleAdapter

pytestmark = pytest.mark.asyncio


@pytest.fixture
def client():
    get_settings.cache_clear()
    return SolanaClient(VannaSettings())  # type: ignore[call-arg]


async def test_refresh_posts_every_pinned_oracle(client):
    adapter = DevForkOracleAdapter(client, client.settings)
    results = await adapter.refresh_all_prices()
    pyth = [r for r in results if r["kind"] == "pyth"]
    scope = [r for r in results if r["kind"] == "scope"]
    assert pyth, "at least one Pyth feed is pinned by the registered assets"
    for row in pyth:
        assert row["value"] > 0
        assert row["source"] in ("jupiter", "previous", "reference")
    assert len(scope) == 1 and scope[0]["entries_restamped"] > 0
    assert scope[0]["source"] in ("mainnet", "fork_restamped")
    await client.close()


async def test_refresh_makes_prices_read_fresh(client, ctx):
    from mcp_server.dispatchers.oracle import vanna_oracle

    adapter = DevForkOracleAdapter(client, client.settings)
    await adapter.refresh_all_prices()
    await client.close()

    result = await vanna_oracle(action="prices", assets=[a.symbol for a in ASSETS])
    for symbol, price in result.items():
        assert price["stale"] is False, f"{symbol} still stale after refresh: {price}"
        assert price["age_seconds"] < 60


async def test_borrow_auto_refreshes_without_manual_step(ctx, funded_fork_keypair):
    """The real end-to-end proof: `vanna_margin_trade.borrow` must succeed
    even when called with no prior manual price refresh — it refreshes
    internally now (this closes the previously-documented StalePrice gap)."""
    from tests._live_helpers import sign_and_submit

    settings = get_settings()
    authority = str(funded_fork_keypair.pubkey())

    from mcp_server.dispatchers.account import vanna_account
    from mcp_server.dispatchers.margin_trade import vanna_margin_trade

    resolved = await vanna_account(action="resolve", authority=authority)
    if not resolved["exists"]:
        envelope = await vanna_account(action="create", authority=authority)
        assert envelope["simulation_success"]
        await sign_and_submit(settings.solana_rpc_url, envelope, funded_fork_keypair)

    state = await vanna_account(action="get", authority=authority)
    if "SOL" not in state["collateral_assets"]:
        envelope = await vanna_margin_trade(action="deposit", authority=authority, asset="SOL", amount="0.2")
        assert envelope["simulation_success"], envelope.get("reason")
        await sign_and_submit(settings.solana_rpc_url, envelope, funded_fork_keypair)

    envelope = await vanna_margin_trade(action="borrow", authority=authority, asset="USDC", amount="0.05")
    assert envelope["simulation_success"], envelope.get("reason")


async def test_default_refresh_keeps_every_asset_fresh_including_pyth_only_jupusd(ctx, monkeypatch):
    """Default source = the app keeper (SOL/USDC/USDT/JupSOL feeds) + the Pyth-only supplement (JupUSD, which
    has no Scope chain and whose feed the keeper does not write). After it, every asset passes every check."""
    from mcp_server.dispatchers.oracle import vanna_oracle
    from vanna_core.contracts import oracle_refresh as refresh

    monkeypatch.setattr(refresh, "_keeper_last_refresh", 0.0)
    monkeypatch.setattr(refresh, "_supplement_last_refresh", 0.0)
    monkeypatch.setattr(refresh, "_keeper_covered_accounts", set())

    settings = get_settings()
    client = SolanaClient(settings)
    try:
        await refresh.maybe_refresh_prices(client, settings)
    finally:
        await client.close()

    plain = ["SOL", "USDC", "USDT", "JitoSOL", "JupSOL", "JupUSD", "NVDAx", "TSLAx"]
    result = await vanna_oracle(action="prices", assets=plain)
    for symbol, price in result.items():
        assert "error" not in price, f"{symbol}: {price}"
        assert price["fresh"] and price["twap_ok"] and price["confidence_ok"], f"{symbol} fails a price check: {price}"
