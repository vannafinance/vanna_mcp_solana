"""Live-devnet integration tests — hits the real deployed vanna_lending program.

No mocks: these confirm the manual Anchor codec (vanna_core/anchor_codec.py,
layouts.py) actually decodes what's on-chain today, and that built
instructions actually simulate successfully against the live program.
"""

from __future__ import annotations

import pytest

from mcp_server.tools.lender_tools import (
    vanna_get_pool_stats,
    vanna_refresh_reserve,
    vanna_supply_liquidity,
)

pytestmark = pytest.mark.asyncio


async def test_get_pool_stats_usdc(ctx):
    result = await vanna_get_pool_stats(asset="USDC")
    assert "error" not in result
    assert result["asset_symbol"] == "USDC"
    assert float(result["total_borrowed"]) >= 0
    assert float(result["utilization_rate_pct"]) >= 0


async def test_get_pool_stats_sol(ctx):
    result = await vanna_get_pool_stats(asset="SOL")
    assert "error" not in result
    assert result["asset_symbol"] == "SOL"


async def test_get_pool_stats_invalid_asset(ctx):
    result = await vanna_get_pool_stats(asset="DOGE")
    assert result["error"] == "invalid_input"


async def test_refresh_reserve_simulates_successfully(ctx, funded_devnet_pubkey):
    # The fee payer must exist on-chain (with a balance) for simulation to
    # succeed, even with sig_verify disabled.
    result = await vanna_refresh_reserve(fee_payer=funded_devnet_pubkey, asset="USDC")
    assert "error" not in result
    assert result["simulation_success"] is True
    assert result["instruction"] == "public_refresh_reserve"


async def test_supply_liquidity_rejects_zero_amount(ctx):
    from solders.keypair import Keypair

    owner = str(Keypair().pubkey())
    result = await vanna_supply_liquidity(owner=owner, asset="USDC", amount="0")
    assert result["error"] == "invalid_input"
