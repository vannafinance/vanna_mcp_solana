"""Live-fork integration tests for `vanna_margin_trade`.

The core happy-path flow (create -> deposit -> borrow -> withdraw -> repay,
each one signed and submitted for real) is exercised by the
`live_margin_position` fixture in conftest.py, which asserts
`simulation_success` at every step before submitting. These tests cover the
dispatcher's input validation and additional edge cases on top of that.
"""

from __future__ import annotations

import pytest
from solders.keypair import Keypair

from mcp_server.dispatchers.margin_trade import vanna_margin_trade

pytestmark = pytest.mark.asyncio


async def test_deposit_missing_args(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_trade(action="deposit", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_withdraw_missing_args(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_trade(action="withdraw", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_borrow_missing_args(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_trade(action="borrow", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_repay_requires_amount_or_repay_all(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_trade(action="repay", authority=fresh, asset="USDC")
    assert result["error"] == "invalid_input"


async def test_repay_from_wallet_requires_payer(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_trade(action="repay_from_wallet", authority=fresh, asset="USDC", amount="1")
    assert result["error"] == "invalid_input"


async def test_unknown_action(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_trade(action="bogus", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_deposit_on_a_wallet_with_no_margin_account_fails_closed(ctx, funded_fork_pubkey):
    # No margin account for this wallet -> the on-chain simulation must fail
    # cleanly (AccountNotFound-style), not crash the dispatcher.
    result = await vanna_margin_trade(action="deposit", authority=funded_fork_pubkey, asset="SOL", amount="0.1")
    assert "schemaVersion" in result
    assert result["simulation_success"] is False


async def test_withdraw_more_than_available_fails_closed(ctx, live_margin_position):
    result = await vanna_margin_trade(
        action="withdraw", authority=live_margin_position, asset="SOL", amount="9999"
    )
    assert result["simulation_success"] is False


async def test_borrow_unreasonably_large_amount_fails_health_check(ctx, live_margin_position):
    result = await vanna_margin_trade(
        action="borrow", authority=live_margin_position, asset="USDC", amount="1000000"
    )
    assert result["simulation_success"] is False


async def test_close_collateral_on_nonempty_vault_fails_closed(ctx, live_margin_position):
    # The fixture's SOL vault is never emptied — closing it while non-empty
    # must fail closed on-chain (NonEmptyVault), not silently succeed.
    result = await vanna_margin_trade(action="close_collateral", authority=live_margin_position, asset="SOL")
    assert "schemaVersion" in result
    assert result["simulation_success"] is False


async def test_close_debt_with_outstanding_debt_fails_closed(ctx, live_margin_position):
    result = await vanna_margin_trade(action="close_debt", authority=live_margin_position, asset="USDC")
    assert "schemaVersion" in result
    assert result["simulation_success"] is False


async def test_repay_from_wallet_with_no_token_account_fails_closed(ctx, live_margin_position, funded_fork_pubkey):
    result = await vanna_margin_trade(
        action="repay_from_wallet",
        authority=live_margin_position,
        payer=funded_fork_pubkey,
        asset="USDC",
        amount="0.1",
    )
    assert "schemaVersion" in result
    assert result["simulation_success"] is False
