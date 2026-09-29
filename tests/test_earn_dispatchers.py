"""Live-fork integration tests for the Earn dispatchers (`vanna_earn_market`,
`vanna_earn_position`, `vanna_earn_write`) — hits the real deployed
vanna_lending program on the hosted Surfpool fork. No mocks: confirms the
manual Anchor codec, the rewritten layouts, and the built instructions
actually decode/simulate successfully against live on-chain state.
"""

from __future__ import annotations

import pytest

from mcp_server.dispatchers.earn_market import vanna_earn_market
from mcp_server.dispatchers.earn_position import vanna_earn_position
from mcp_server.dispatchers.earn_write import vanna_earn_write

pytestmark = pytest.mark.asyncio


async def test_pool_stats_usdc(ctx):
    result = await vanna_earn_market(action="pool_stats", asset="USDC")
    assert "error" not in result
    assert result["asset_symbol"] == "USDC"
    assert float(result["total_borrowed"]) >= 0
    assert float(result["utilization_rate_pct"]) >= 0
    assert result["status"] in (0, 1, 2, 3)


async def test_pool_stats_every_earn_pool(ctx):
    for symbol in ("SOL", "USDC", "USDT"):
        result = await vanna_earn_market(action="pool_stats", asset=symbol)
        assert "error" not in result, f"{symbol}: {result}"
        assert result["asset_symbol"] == symbol


async def test_pool_stats_collateral_only_assets_have_no_earn_pool(ctx):
    # JitoSOL/JupSOL/JupUSD/xStocks are collateral-only (D2): no Reserve exists on-chain.
    for symbol in ("JitoSOL", "JupSOL"):
        result = await vanna_earn_market(action="pool_stats", asset=symbol)
        assert result["error"] == "asset_not_offered", f"{symbol}: {result}"


async def test_pool_stats_invalid_asset(ctx):
    result = await vanna_earn_market(action="pool_stats", asset="DOGE")
    assert result["error"] == "invalid_input"


async def test_pool_stats_unknown_action(ctx):
    result = await vanna_earn_market(action="not_a_real_action", asset="USDC")
    assert result["error"] == "invalid_input"


async def test_deposited_balance_zero_for_fresh_wallet(ctx):
    from solders.keypair import Keypair

    owner = str(Keypair().pubkey())
    result = await vanna_earn_position(action="deposited_balance", owner=owner, asset="USDC")
    assert result["share_balance"] == "0"
    assert result["redeemable_underlying"] == "0"


async def test_earned_interest_fresh_wallet_is_zero(ctx):
    from solders.keypair import Keypair

    owner = str(Keypair().pubkey())
    result = await vanna_earn_position(action="earned_interest", owner=owner, asset="USDC")
    assert "error" not in result
    assert float(result["earned_interest"]) == 0
    assert float(result["net_principal_contributed"]) == 0


async def test_supply_rejects_zero_amount(ctx):
    from solders.keypair import Keypair

    owner = str(Keypair().pubkey())
    result = await vanna_earn_write(action="supply", owner=owner, asset="USDC", amount="0")
    assert result["error"] == "invalid_input"


async def test_supply_requires_amount(ctx):
    from solders.keypair import Keypair

    owner = str(Keypair().pubkey())
    result = await vanna_earn_write(action="supply", owner=owner, asset="USDC")
    assert result["error"] == "invalid_input"


async def test_supply_sol_simulates_successfully(ctx, funded_fork_pubkey):
    # A funded wallet supplying native SOL: the wsol.py wrap prefix plus
    # lender_supply must simulate cleanly end-to-end. Envelope dicts always
    # carry an "error" key (None on success) — check simulation_success, not
    # key presence (that "error" not in result check is only valid for the
    # tool_safe wrapper's ad hoc error dicts, not the schema-v1 envelope).
    result = await vanna_earn_write(action="supply", owner=funded_fork_pubkey, asset="SOL", amount="0.1")
    assert result.get("code") is None, result
    assert result["simulation_success"] is True, result.get("reason")
    assert result["transactions"][0]["purpose"] == "lender_supply"


async def test_redeem_with_no_token_account_fails_closed_not_crashes(ctx, funded_fork_pubkey):
    # No Earn position yet -> the on-chain lender token/share accounts don't
    # exist. Simulation must report a clean failure, not raise past the tool
    # boundary or return the ad hoc {"error": ...} shape from @tool_safe.
    result = await vanna_earn_write(action="redeem", owner=funded_fork_pubkey, asset="USDC", shares="1")
    assert "schemaVersion" in result  # a real envelope came back, not a tool_safe error dict
    assert result["simulation_success"] is False
    assert result["code"] == "simulation_failed"


async def test_supply_sol_live_then_position_and_history_reflect_it(ctx, funded_fork_keypair):
    from tests._live_helpers import sign_and_submit
    from vanna_core.config import get_settings as _get_settings

    settings = _get_settings()
    owner = str(funded_fork_keypair.pubkey())

    envelope = await vanna_earn_write(action="supply", owner=owner, asset="SOL", amount="0.2")
    assert envelope["simulation_success"], envelope.get("reason")
    await sign_and_submit(settings.solana_rpc_url, envelope, funded_fork_keypair)

    balance = await vanna_earn_position(action="deposited_balance", owner=owner, asset="SOL")
    assert float(balance["share_balance"]) > 0
    assert float(balance["redeemable_underlying"]) > 0

    earned = await vanna_earn_position(action="earned_interest", owner=owner, asset="SOL")
    assert "error" not in earned
    assert float(earned["net_principal_contributed"]) == pytest.approx(0.2, abs=1e-6)

    from mcp_server.dispatchers.history import vanna_history

    history = await vanna_history(action="earn", authority=owner, asset="SOL")
    assert any(e["event"] == "LiquiditySupplied" for e in history["events"])
