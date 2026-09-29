"""Live-fork integration tests for `vanna_margin_status`."""

from __future__ import annotations

import pytest
from solders.keypair import Keypair

from mcp_server.dispatchers.margin_status import vanna_margin_status

pytestmark = pytest.mark.asyncio


async def test_collateral_on_fresh_wallet_fails_closed(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_status(action="collateral", authority=fresh)
    assert result["error"] == "account_not_found"


async def test_can_borrow_missing_args(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_status(action="can_borrow", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_can_withdraw_missing_args(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_status(action="can_withdraw", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_unknown_action(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_status(action="bogus", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_collateral_real_position(ctx, live_margin_position):
    result = await vanna_margin_status(action="collateral", authority=live_margin_position)
    assert "error" not in result
    symbols = {row["symbol"] for row in result["collateral"]}
    assert "SOL" in symbols


async def test_debt_real_position(ctx, live_margin_position):
    result = await vanna_margin_status(action="debt", authority=live_margin_position)
    assert "error" not in result
    assert any(row["symbol"] == "USDC" for row in result["debt"])


async def test_can_borrow_amount_exceeding_pool_liquidity_denied(ctx, live_margin_position):
    # NOTE: USDC is collateral_enabled, so borrowed USDC also counts as
    # collateral (the protocol's own rule) — a single-asset borrow is
    # self-collateralizing and only breaches the 1.10 health factor at very
    # large amounts. Pool liquidity (a few thousand USDC) is a far tighter,
    # more reliable constraint to test against a live, evolving pool than
    # picking a number meant to land just past the health-factor threshold.
    result = await vanna_margin_status(
        action="can_borrow", authority=live_margin_position, asset="USDC", amount="1000000"
    )
    assert result["allowed"] is False
    assert "liquidity" in result["reason"]


async def test_can_borrow_small_amount_allowed(ctx, live_margin_position):
    result = await vanna_margin_status(
        action="can_borrow", authority=live_margin_position, asset="USDC", amount="0.01"
    )
    assert result["allowed"] is True


async def test_can_withdraw_small_amount_allowed(ctx, live_margin_position):
    result = await vanna_margin_status(
        action="can_withdraw", authority=live_margin_position, asset="SOL", amount="0.001"
    )
    assert result["allowed"] is True


async def test_can_withdraw_no_position_for_asset(ctx, live_margin_position):
    result = await vanna_margin_status(
        action="can_withdraw", authority=live_margin_position, asset="JitoSOL", amount="1"
    )
    assert result["allowed"] is False
    assert "no active" in result["reason"]


async def test_liquidation_snapshot_healthy_account_not_liquidatable(ctx, live_margin_position):
    result = await vanna_margin_status(action="liquidation_snapshot", authority=live_margin_position)
    assert "error" not in result
    assert result["is_liquidatable"] is False
    # Whole-account liquidation: no close factor - the liquidator repays every debt in full
    # and receives every collateral balance.
    assert result["liquidation_model"] == "whole_account"
    assert "close_factor_bps" not in result
    assert len(result["debt_to_repay"]) >= 1
    assert len(result["collateral_to_seize"]) >= 1
    assert float(result["estimated_reward_usd"].lstrip("-")) >= 0
