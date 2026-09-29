"""Live: Token-2022 collateral (the xStocks NVDAx / TSLAx). Every earlier live flow used classic SPL tokens,
so this proves the token-program handling (wallet ATA creation, deposit, Scope-priced valuation, withdraw)."""

from __future__ import annotations

import pytest
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair

from mcp_server.dispatchers.account import vanna_account
from mcp_server.dispatchers.margin_status import vanna_margin_status
from mcp_server.dispatchers.margin_trade import vanna_margin_trade
from mcp_server.dispatchers.wallet import vanna_wallet
from tests._live_helpers import build_with_retry, fund_token, sign_and_submit

pytestmark = pytest.mark.asyncio

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


async def _fresh_wallet(rpc_url: str) -> Keypair:
    kp = Keypair()
    async with AsyncClient(rpc_url, commitment=Confirmed) as client:
        sig = (await client.request_airdrop(kp.pubkey(), 2_000_000_000)).value
        await client.confirm_transaction(sig, commitment=Confirmed)
    return kp


@pytest.mark.parametrize("symbol", ["NVDAx", "TSLAx"])
async def test_token2022_xstock_deposit_value_and_withdraw(ctx, symbol):
    settings = ctx.settings
    meta = await vanna_wallet(action="token_metadata", authority="11111111111111111111111111111111", asset=symbol)
    assert meta["token_2022"] is True and meta["token_program"] == TOKEN_2022
    decimals = meta["decimals"]

    kp = await _fresh_wallet(settings.solana_rpc_url)
    authority = str(kp.pubkey())
    await fund_token(settings.solana_rpc_url, authority, meta["mint"], 5 * 10**decimals, TOKEN_2022)
    assert (await vanna_wallet(action="balance", authority=authority, asset=symbol))["ui_balance"] == "5"

    created = await vanna_account(action="create", authority=authority)
    assert created["simulation_success"], created.get("reason")
    await sign_and_submit(settings.solana_rpc_url, created, kp)

    deposit = await vanna_margin_trade(action="deposit", authority=authority, asset=symbol, amount="1")
    assert deposit["simulation_success"], deposit.get("reason")
    await sign_and_submit(settings.solana_rpc_url, deposit, kp)

    # Valued through the program's own oracle rules (Scope chain), fresh, no debt -> healthy.
    health = await vanna_account(action="health", authority=authority)
    row = next(r for r in health["collateral"] if r["symbol"] == symbol)
    assert row["ui_balance"] == "1"
    assert float(row["usd_value"]) > 1  # a share of a US stock trades well above $1
    assert row["price"]["source"] == "scope" and row["price"]["fresh"]
    assert health["is_healthy"] is True

    # With no debt the program reads no prices for a withdrawal.
    max_withdraw = await vanna_margin_status(action="max_withdraw", authority=authority, asset=symbol)
    assert max_withdraw["max_amount"] == "1"
    withdraw = await build_with_retry(
        lambda: vanna_margin_trade(action="withdraw", authority=authority, asset=symbol, amount="1")
    )
    assert withdraw["simulation_success"], withdraw.get("reason")
    await sign_and_submit(settings.solana_rpc_url, withdraw, kp)
    assert (await vanna_wallet(action="balance", authority=authority, asset=symbol))["ui_balance"] == "5"
