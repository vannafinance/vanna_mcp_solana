"""Live-fork integration tests for `vanna_account`."""

from __future__ import annotations

import pytest
from solders.keypair import Keypair

from mcp_server.dispatchers.account import vanna_account

pytestmark = pytest.mark.asyncio


async def test_resolve_fresh_wallet_has_no_margin_account(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_account(action="resolve", authority=fresh)
    assert result["exists"] is False
    assert "margin_account" in result


async def test_get_on_fresh_wallet_fails_closed(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_account(action="get", authority=fresh)
    assert result["error"] == "account_not_found"


async def test_health_on_fresh_wallet_fails_closed(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_account(action="health", authority=fresh)
    assert result["error"] == "account_not_found"


async def test_invalid_address(ctx):
    result = await vanna_account(action="resolve", authority="not-a-real-address")
    assert result["error"] == "invalid_input"


async def test_unknown_action(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_account(action="bogus", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_resolve_exists_for_a_real_margin_account(ctx, live_margin_position):
    result = await vanna_account(action="resolve", authority=live_margin_position)
    assert result["exists"] is True


async def test_get_reports_real_collateral_and_debt(ctx, live_margin_position):
    result = await vanna_account(action="get", authority=live_margin_position)
    assert "error" not in result
    assert result["status"] == "Active"
    assert "SOL" in result["collateral_assets"]
    assert "USDC" in result["debt_assets"]


async def test_health_computes_real_values(ctx, live_margin_position):
    result = await vanna_account(action="health", authority=live_margin_position)
    assert "error" not in result
    assert result["is_healthy"] is True
    assert result["is_liquidatable"] is False
    assert float(result["total_collateral_usd"]) > float(result["total_debt_usd"])
    assert float(result["health_factor"]) > 1.10
    symbols = {row["symbol"] for row in result["collateral"]}
    assert "SOL" in symbols
    debt_symbols = {row["symbol"] for row in result["debt"]}
    assert debt_symbols == {"USDC"}


async def test_create_margin_for_an_existing_account_reports_simulation_failure(ctx, live_margin_position):
    # Re-creating an already-existing margin account must fail closed at
    # simulation, not silently succeed or crash.
    result = await vanna_account(action="create", authority=live_margin_position)
    assert "schemaVersion" in result
    assert result["simulation_success"] is False
