"""Assets registered on-chain but not in the product (team, 2026-09-30): JupUSD and the Token-2022 xStocks
NVDAx/TSLAx. They must be hidden from listings and refused for every action with `asset_not_offered`,
while a position in one (e.g. created outside the app) still counts in health and can be withdrawn."""

from __future__ import annotations

import pytest
from solders.keypair import Keypair

from mcp_server.dispatchers.margin_status import vanna_margin_status
from mcp_server.dispatchers.margin_trade import vanna_margin_trade
from mcp_server.dispatchers.oracle import vanna_oracle
from mcp_server.dispatchers.protocol_info import vanna_protocol_info
from mcp_server.dispatchers.swap import vanna_swap
from mcp_server.dispatchers.wallet import vanna_wallet

pytestmark = pytest.mark.asyncio

UNOFFERED = ("JupUSD", "NVDAx", "TSLAx")


async def test_not_listed(ctx):
    listed = {a["symbol"] for a in (await vanna_protocol_info(action="assets"))["assets"]}
    assert not listed & set(UNOFFERED)
    wallet = await vanna_wallet(action="summary", authority=str(Keypair().pubkey()))
    assert not {a["symbol"] for a in wallet["assets"]} & set(UNOFFERED)


@pytest.mark.parametrize("symbol", UNOFFERED)
async def test_refused_for_actions_and_reads(ctx, symbol):
    fresh = str(Keypair().pubkey())
    assert (await vanna_margin_trade(action="deposit", authority=fresh, asset=symbol, amount="1"))["error"] == "asset_not_offered"
    assert (await vanna_margin_trade(action="borrow", authority=fresh, asset=symbol, amount="1"))["error"] == "asset_not_offered"
    assert (await vanna_swap(action="quote", from_asset="SOL", to_asset=symbol, amount="1"))["error"] == "asset_not_offered"
    assert (await vanna_oracle(action="price", asset=symbol))["error"] == "asset_not_offered"
    assert (await vanna_wallet(action="balance", authority=fresh, asset=symbol))["error"] == "asset_not_offered"
    prices = await vanna_oracle(action="prices", assets=["SOL", symbol])
    assert "error" not in prices["SOL"] and prices[symbol]["error"] == "invalid_input"


async def test_exit_paths_still_resolve_an_unoffered_asset(ctx, live_margin_position):
    # Withdraw/close must never be blocked by the product scope: here the account holds no NVDAx, so the
    # answer is "no position", not "not offered".
    result = await vanna_margin_status(action="max_withdraw", authority=live_margin_position, asset="NVDAx")
    assert "error" not in result and result["max_amount"] == "0" and "no active" in result["reason"]
