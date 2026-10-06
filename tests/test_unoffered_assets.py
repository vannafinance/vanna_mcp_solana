"""Product scope on the live chain: every token the app lists (the catalogue plus JupUSD and the Token-2022 xStocks)
is listed and usable as collateral; tokens outside the product are refused; the app's two action rules hold (USDT is
not a collateral deposit, the JitoSOL/JupSOL Kamino farms are closed to new supply); and exit paths still resolve any
registered asset so nobody is stuck."""

from __future__ import annotations

import pytest
from solders.keypair import Keypair

from mcp_server.dispatchers.farm_kamino import vanna_farm_kamino
from mcp_server.dispatchers.margin_status import vanna_margin_status
from mcp_server.dispatchers.margin_trade import vanna_margin_trade
from mcp_server.dispatchers.oracle import vanna_oracle
from mcp_server.dispatchers.protocol_info import vanna_protocol_info

pytestmark = pytest.mark.asyncio

NEW_COLLATERAL = ("JupUSD", "NVDAx", "TSLAx", "GOOGLx", "AAPLx")


async def test_new_collateral_tokens_are_listed_and_priced(ctx):
    listed = {a["symbol"] for a in (await vanna_protocol_info(action="assets"))["assets"]}
    assert set(NEW_COLLATERAL) <= listed
    assert "GMTrade" not in listed
    prices = await vanna_oracle(action="prices", assets=list(NEW_COLLATERAL))
    for symbol in NEW_COLLATERAL:
        assert "error" not in prices[symbol], (symbol, prices[symbol])


@pytest.mark.parametrize("symbol", NEW_COLLATERAL)
async def test_new_collateral_tokens_cannot_be_borrowed(ctx, symbol):
    fresh = str(Keypair().pubkey())
    assert (await vanna_margin_trade(action="borrow", authority=fresh, asset=symbol, amount="1"))["error"] == "asset_not_offered"


async def test_usdt_is_not_a_collateral_deposit(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_trade(action="deposit", authority=fresh, asset="USDT", amount="1")
    assert result["error"] == "asset_not_offered" and "deposit" in result["message"]


@pytest.mark.parametrize("underlying", ("JitoSOL", "JupSOL"))
async def test_delisted_farms_refuse_new_supply(ctx, underlying):
    fresh = str(Keypair().pubkey())
    result = await vanna_farm_kamino(action="deposit", authority=fresh, underlying=underlying, amount="1")
    assert result["error"] == "asset_not_offered" and "closed to new supply" in result["message"]


async def test_tokens_outside_the_product_are_refused(ctx):
    assert "error" in await vanna_oracle(action="price", asset="BONK")


async def test_exit_paths_still_resolve_any_registered_asset(ctx, live_margin_position):
    # Withdraw/close must never be blocked by the product scope: here the account holds no NVDAx, so the
    # answer is "no position", not "not offered".
    result = await vanna_margin_status(action="max_withdraw", authority=live_margin_position, asset="NVDAx")
    assert "error" not in result and result["max_amount"] == "0" and "no active" in result["reason"]
