"""Live-fork integration tests for `vanna_wallet`."""

from __future__ import annotations

import pytest
from solders.keypair import Keypair

from mcp_server.dispatchers.wallet import vanna_wallet

pytestmark = pytest.mark.asyncio


async def test_summary_fresh_wallet_all_zero(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_wallet(action="summary", authority=fresh)
    assert result["sol_balance"] == "0"
    assert result["margin_account_exists"] is False
    # Every registered non-receipt asset: SOL, USDC, USDT, JitoSOL, JupSOL, JupUSD, NVDAx, TSLAx.
    assert {a["symbol"] for a in result["assets"]} == {
        "SOL", "USDC", "USDT", "JitoSOL", "JupSOL", "JupUSD", "NVDAx", "TSLAx"
    }
    assert all(a["wallet_balance"] == "0" for a in result["assets"])
    by_symbol = {a["symbol"]: a for a in result["assets"]}
    assert by_symbol["USDC"]["lender_share_balance"] == "0"  # Earn pool exists
    assert by_symbol["JitoSOL"]["lender_share_balance"] is None  # collateral-only: no Earn pool


async def test_summary_funded_wallet_has_positive_sol(ctx, funded_fork_pubkey):
    result = await vanna_wallet(action="summary", authority=funded_fork_pubkey)
    assert float(result["sol_balance"]) > 0


async def test_summary_reports_real_margin_account(ctx, live_margin_position):
    result = await vanna_wallet(action="summary", authority=live_margin_position)
    assert result["margin_account_exists"] is True


async def test_balance_requires_asset(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_wallet(action="balance", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_balance_invalid_asset(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_wallet(action="balance", authority=fresh, asset="DOGE")
    assert result["error"] == "invalid_input"


async def test_token_metadata_usdc(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_wallet(action="token_metadata", authority=fresh, asset="USDC")
    assert result["decimals"] == 6
    assert result["mint"] == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert float(result["supply_ui"]) > 0


async def test_unknown_action(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_wallet(action="bogus", authority=fresh)
    assert result["error"] == "invalid_input"
