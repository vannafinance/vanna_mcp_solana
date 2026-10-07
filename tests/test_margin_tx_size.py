"""Margin transactions stay inside Solana's 1232-byte packet for an account holding many assets.

A borrow or withdrawal names every position's config, mint, token program and oracle accounts for the health check.
On 2026-10-07 a SOL borrow on an account holding GOOGLx (a Token-2022 xStock) and several other assets measured 1276
bytes and was refused by the network. Margin transactions now load the protocol-wide keys from the protocol lookup
table (`MarginTradeContract._envelope`); this test rebuilds that account on the fork and checks the borrow and a
withdrawal both build, simulate and fit.
"""

from __future__ import annotations

import base64

import pytest
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from mcp_server.dispatchers.account import vanna_account
from mcp_server.dispatchers.margin_trade import vanna_margin_trade
from tests._live_helpers import build_with_retry, fund_token, sign_and_submit
from tests.conftest import _airdrop
from vanna_core.assets import AssetRegistry

pytestmark = pytest.mark.asyncio

PACKET_LIMIT = 1232
# Every collateral-only product token (the xStocks need the most accounts) plus the stables.
HELD = ("USDC", "JitoSOL", "JupSOL", "JupUSD", "NVDAx", "TSLAx", "AAPLx", "GOOGLx")


def tx_size(envelope: dict) -> int:
    raw = base64.b64decode(envelope["transactions"][0]["unsignedTransaction"])
    return len(bytes(VersionedTransaction.from_bytes(raw)))


async def test_borrow_and_withdraw_fit_with_many_assets_held(ctx):
    if not ctx.settings.protocol_lookup_tables:
        pytest.skip("PROTOCOL_LOOKUP_TABLES is not set")
    rpc_url = ctx.settings.solana_rpc_url
    kp = Keypair()
    owner = str(kp.pubkey())
    await _airdrop(rpc_url, kp.pubkey(), 5_000_000_000)

    created = await build_with_retry(lambda: vanna_account(action="create", authority=owner))
    await sign_and_submit(rpc_url, created, kp)

    registry = AssetRegistry(ctx.client)
    await registry.load()
    for symbol in HELD:
        asset = registry.by_symbol(symbol)
        await fund_token(rpc_url, owner, asset.mint, 10 * 10**asset.decimals, asset.token_program)
        deposit = await build_with_retry(lambda s=symbol: vanna_margin_trade(action="deposit", authority=owner, asset=s, amount="1"))
        assert deposit.get("simulation_success"), (symbol, deposit.get("reason"))
        assert tx_size(deposit) <= PACKET_LIMIT, (symbol, tx_size(deposit))
        await sign_and_submit(rpc_url, deposit, kp)

    borrow = await build_with_retry(lambda: vanna_margin_trade(action="borrow", authority=owner, asset="SOL", amount="0.05"))
    assert borrow.get("error") is None, borrow
    assert borrow.get("simulation_success"), borrow.get("reason")
    assert tx_size(borrow) <= PACKET_LIMIT, tx_size(borrow)

    withdraw = await build_with_retry(lambda: vanna_margin_trade(action="withdraw", authority=owner, asset="GOOGLx", amount="0.5"))
    assert withdraw.get("error") is None, withdraw
    assert tx_size(withdraw) <= PACKET_LIMIT, tx_size(withdraw)
