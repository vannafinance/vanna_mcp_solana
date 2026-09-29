"""Live-fork integration tests for `vanna_farm_overview` and `vanna_farm_kamino`."""

from __future__ import annotations

import pytest
from solders.keypair import Keypair

from mcp_server.dispatchers.farm_kamino import vanna_farm_kamino
from mcp_server.dispatchers.farm_overview import vanna_farm_overview

pytestmark = pytest.mark.asyncio


async def test_markets_lists_all_five_receipts(ctx):
    result = await vanna_farm_overview(action="markets")
    assert "error" not in result
    symbols = {m["symbol"] for m in result["markets"]}
    assert symbols == {"kSOL", "kUSDC", "kUSDT", "kJitoSOL", "kJupSOL"}
    for market in result["markets"]:
        # Kamino cTokens don't start near 1:1 with the underlying (unlike
        # some other protocols' receipt tokens) — only that the rate is a
        # real positive number is guaranteed.
        assert float(market["exchange_rate"]) > 0
        assert market["metrics_available"] is True  # Kamino's live API, confirmed reachable


async def test_positions_fresh_wallet_empty(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_farm_overview(action="positions", authority=fresh)
    assert result["error"] == "account_not_found"


async def test_positions_requires_authority(ctx):
    result = await vanna_farm_overview(action="positions")
    assert result["error"] == "invalid_input"


async def test_unknown_action(ctx):
    result = await vanna_farm_overview(action="bogus")
    assert result["error"] == "invalid_input"


async def test_deposit_unknown_underlying(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_farm_kamino(action="deposit", authority=fresh, underlying="DOGE", amount="1")
    assert result["error"] == "invalid_input"


async def test_deposit_without_active_collateral_fails_closed(ctx, funded_fork_pubkey):
    # No margin account at all for this wallet -> fails closed before even
    # reaching simulation (the ad hoc @tool_safe error shape, not an envelope
    # — there's no margin account to build a real transaction against).
    result = await vanna_farm_kamino(action="deposit", authority=funded_fork_pubkey, underlying="SOL", amount="0.1")
    assert result["error"] == "account_not_found"


async def test_farm_deposit_then_redeem_live(ctx, funded_fork_keypair, live_margin_position):
    from tests._live_helpers import sign_and_submit
    from vanna_core.config import get_settings as _get_settings

    settings = _get_settings()
    authority = live_margin_position  # same wallet as funded_fork_keypair

    from tests._live_helpers import build_with_retry

    deposit_env = await build_with_retry(
        lambda: vanna_farm_kamino(action="deposit", authority=authority, underlying="SOL", amount="0.1")
    )
    assert deposit_env["simulation_success"], deposit_env.get("reason")
    await sign_and_submit(settings.solana_rpc_url, deposit_env, funded_fork_keypair)

    positions = await vanna_farm_overview(action="positions", authority=authority)
    assert "error" not in positions
    ksol = next((p for p in positions["positions"] if p["symbol"] == "kSOL"), None)
    assert ksol is not None
    assert float(ksol["receipt_balance"]) > 0
    assert float(ksol["underlying_amount"]) == pytest.approx(0.1, abs=1e-6)

    redeem_env = await build_with_retry(
        lambda: vanna_farm_kamino(action="redeem", authority=authority, underlying="SOL", amount=ksol["receipt_balance"])
    )
    assert redeem_env["simulation_success"], redeem_env.get("reason")
    await sign_and_submit(settings.solana_rpc_url, redeem_env, funded_fork_keypair)

    positions_after = await vanna_farm_overview(action="positions", authority=authority)
    remaining = next((p for p in positions_after["positions"] if p["symbol"] == "kSOL"), None)
    assert remaining is None or float(remaining["receipt_balance"]) == 0


async def test_account_health_includes_kamino_collateral(ctx, funded_fork_keypair, live_margin_position):
    from tests._live_helpers import build_with_retry, sign_and_submit
    from mcp_server.dispatchers.account import vanna_account
    from vanna_core.config import get_settings as _get_settings

    settings = _get_settings()
    authority = live_margin_position

    deposit_env = await build_with_retry(
        lambda: vanna_farm_kamino(action="deposit", authority=authority, underlying="SOL", amount="0.05")
    )
    assert deposit_env["simulation_success"], deposit_env.get("reason")
    await sign_and_submit(settings.solana_rpc_url, deposit_env, funded_fork_keypair)

    health = await vanna_account(action="health", authority=authority)
    assert "error" not in health
    ksol_row = next((r for r in health["collateral"] if r["symbol"] == "kSOL"), None)
    assert ksol_row is not None
    assert float(ksol_row["usd_value"]) > 0
    assert health["is_healthy"] is True


async def test_oracle_prices_ksol_using_exchange_rate(ctx):
    from mcp_server.dispatchers.oracle import vanna_oracle

    result = await vanna_oracle(action="price", asset="kSOL")
    assert "error" not in result
    assert result["source"] == "kamino_receipt"
    assert result["underlying_symbol"] == "SOL"
    # kSOL's cToken exchange rate is far below 1:1 with SOL (a large-supply,
    # low-unit-value receipt design) — verified against a live deposit
    # (0.5 SOL in -> 433.3 kSOL out), so price_usd is a small fraction of
    # underlying_price_usd, not a multiple of it.
    assert 0 < float(result["price_usd"]) < float(result["underlying_price_usd"])
