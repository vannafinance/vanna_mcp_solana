"""Live-fork integration tests for `vanna_history`."""

from __future__ import annotations

import pytest
from solders.keypair import Keypair

from mcp_server.dispatchers.history import vanna_history

pytestmark = pytest.mark.asyncio


async def test_margin_history_fresh_wallet_empty(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_history(action="margin", authority=fresh)
    assert "error" not in result
    assert result["events"] == []


async def test_earn_history_requires_asset(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_history(action="earn", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_unknown_action(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_history(action="bogus", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_margin_history_reports_real_events(ctx, live_margin_position):
    result = await vanna_history(action="margin", authority=live_margin_position, limit=50)
    assert "error" not in result
    event_names = {e["event"] for e in result["events"]}
    # The fixture does create -> deposit(SOL) -> borrow(USDC), at minimum.
    assert "MarginCreated" in event_names
    assert "CollateralDeposited" in event_names
    assert "Borrowed" in event_names
    borrow_row = next(e for e in result["events"] if e["event"] == "Borrowed")
    assert borrow_row["asset"] == "USDC"
    assert float(borrow_row["amount"]) > 0
    assert "post_action_health_factor" in borrow_row


async def test_earn_history_no_deposits_for_this_wallet(ctx, live_margin_position):
    # The margin fixture wallet never used vanna_earn_write directly.
    result = await vanna_history(action="earn", authority=live_margin_position, asset="USDC")
    assert "error" not in result
    assert result["events"] == []
