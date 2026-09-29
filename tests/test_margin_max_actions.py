"""`vanna_margin_status` max_borrow / max_withdraw — sizing answers that must
agree with `can_borrow` / `can_withdraw` (accepted at the max, refused above)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from mcp_server.dispatchers.margin_status import vanna_margin_status

pytestmark = pytest.mark.asyncio


async def test_max_borrow_accepted_and_above_refused(ctx, live_margin_position):
    result = await vanna_margin_status(action="max_borrow", authority=live_margin_position, asset="USDC")
    assert "error" not in result
    max_amount = Decimal(result["max_amount"])
    assert max_amount > 0

    at_max = await vanna_margin_status(
        action="can_borrow", authority=live_margin_position, asset="USDC", amount=result["max_amount"]
    )
    assert at_max["allowed"] is True

    above = await vanna_margin_status(
        action="can_borrow", authority=live_margin_position, asset="USDC", amount=str(max_amount * 2 + 1)
    )
    assert above["allowed"] is False


async def test_max_borrow_rejects_collateral_only_asset(ctx, live_margin_position):
    result = await vanna_margin_status(action="max_borrow", authority=live_margin_position, asset="JupSOL")
    assert result["error"] == "asset_not_offered"


async def test_max_withdraw_accepted_by_can_withdraw(ctx, live_margin_position):
    result = await vanna_margin_status(action="max_withdraw", authority=live_margin_position, asset="SOL")
    assert "error" not in result
    assert Decimal(result["max_amount"]) > 0

    at_max = await vanna_margin_status(
        action="can_withdraw", authority=live_margin_position, asset="SOL", amount=result["max_amount"]
    )
    assert at_max["allowed"] is True


async def test_max_withdraw_without_that_collateral_reports_zero(ctx, live_margin_position):
    result = await vanna_margin_status(action="max_withdraw", authority=live_margin_position, asset="JupSOL")
    assert result["max_amount"] == "0"
    assert "no active" in result["reason"]


@pytest.mark.parametrize("action", ["max_borrow", "max_withdraw"])
async def test_max_actions_require_asset(ctx, live_margin_position, action):
    result = await vanna_margin_status(action=action, authority=live_margin_position)
    assert result["error"] == "invalid_input"
