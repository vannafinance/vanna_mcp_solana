"""Phoenix perps reads (vanna_core/contracts/perps.py): the app's preview sizing, caps and liquidation price. Offline: the
margin snapshot and the Phoenix venue are faked."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vanna_core.contracts.perps import PerpsContract, default_slippage_bps, market_key
from vanna_core.phoenix import FULL_TRADER_CAPABILITIES, PhoenixPosition, PhoenixTrader

NANO = 1_000_000_000


def _market(key="SOL", asset_id=0, max_lev_bps=50_000, enabled=True, mark=100.0, index=100.0, lot_decimals=2):
    return SimpleNamespace(
        meta=SimpleNamespace(key=key, name=f"{key}-PERP"),
        entry=SimpleNamespace(asset_id=asset_id, max_leverage_bps=max_lev_bps, trading_enabled=enabled),
        market=SimpleNamespace(lot_decimals=lot_decimals),
        orderbook=SimpleNamespace(taker_fee_micro=350),
        index_usd=index,
        mark_usd=mark,
        index_fresh=True,
    )


def _contract(*, usdc_raw=100_000_000, collateral_usd=1000.0, debt_usd=0.0, trader=None, venue_value=0.0, positions=(), markets=None):
    c = PerpsContract.__new__(PerpsContract)
    c._client = SimpleNamespace(settings=SimpleNamespace(liquidation_health_factor=1.10))
    health = SimpleNamespace(
        total_collateral_value=int(collateral_usd * NANO), total_debt_value=int(debt_usd * NANO),
        health_factor_str=None if debt_usd == 0 else f"{collateral_usd / debt_usd:.4f}",
    )
    snap = SimpleNamespace(health=health, collateral_rows=[{"symbol": "USDC", "raw_balance": usdc_raw, "ui_balance": str(usdc_raw / 1e6)}])
    venue = SimpleNamespace(
        markets=markets or [_market()], trader=trader, value_nano=int(venue_value * NANO), positions=list(positions),
        collateral_usd=0.0, withdrawable_now=0,
    )

    async def account(_authority):
        return snap, venue

    c._account = account
    return c


def test_market_names_and_default_slippage():
    assert market_key(" sol-perp ") == "SOL"
    assert (default_slippage_bps("SOL"), default_slippage_bps("TSLA-PERP")) == (100, 200)


async def test_preview_sizes_collateral_times_leverage_at_the_limit_price():
    r = await _contract().preview(None, "SOL", "long", "10", 3)
    assert r["allowed"] and r["limit_price"] == "101.000000"  # mark 100 + 1%
    assert r["size"] == "0.290000"  # $30 / $101 = 0.297 SOL, floored to 0.01-SOL lots
    assert r["needs_trader_registration"] is True


@pytest.mark.parametrize(
    "kwargs, args, reason",
    [
        ({}, ("SOL", "long", "10", 6), "between 1x and 5x"),
        ({"markets": [_market(enabled=False)]}, ("SOL", "long", "10", 2), "closed for new positions"),
        ({"usdc_raw": 5_000_000}, ("SOL", "long", "10", 2), "Not enough USDC"),
        ({}, ("DOGE", "long", "10", 2), "not a listed Phoenix market"),
        ({}, ("SOL", "long", "0", 2), "Add USDC"),
        ({}, ("SOL", "long", "0.001", 1), "Size too small"),
    ],
)
async def test_preview_refuses_like_the_app(kwargs, args, reason):
    r = await _contract(**kwargs).preview(None, *args)
    assert r["allowed"] is False and reason in r["reason"]


async def test_preview_keeps_inside_the_cap_with_other_markets_exposure():
    trader = PhoenixTrader(
        collateral=20_000_000, capabilities=FULL_TRADER_CAPABILITIES, hot=False, withdraw_queued=False,
        positions=(PhoenixPosition(asset_id=1, base_lots=10, virtual_quote_lots=-120_000_000, funding_snapshot=0),),
    )
    r = await _contract(trader=trader, markets=[_market(), _market("ETH", asset_id=1)]).preview(None, "SOL", "long", "10", 5)
    # Phoenix collateral 30 USDC x 5 = $150 cap, less $120 already open on ETH: $50 asked, about $30 fits at the limit.
    assert r["allowed"] and r["reduced_to_fit_cap"] and float(r["size"]) * 101 <= 30
    assert r["needs_trader_registration"] is False


async def test_liquidation_price_needs_debt_and_must_be_reachable():
    r = await _contract(collateral_usd=1000.0, debt_usd=850.0, venue_value=500.0).preview(None, "SOL", "long", "50", 4)
    assert r["allowed"] and r["projected_health_factor"] is not None
    liq = float(r["liquidation_price_after"])
    assert 0 < liq < 100  # a long is liquidated below the price
    no_debt = await _contract().preview(None, "SOL", "long", "50", 4)
    assert no_debt["liquidation_price_after"] is None


def test_margin_model_tracks_slots_like_the_program():
    from vanna_core.layouts import EMPTY_ASSET_INDEX
    from vanna_core.margin_execute import MarginModel

    raw = SimpleNamespace(collateral_asset_indexes=[1, EMPTY_ASSET_INDEX, EMPTY_ASSET_INDEX], debt_asset_indexes=[EMPTY_ASSET_INDEX] * 3)
    m = MarginModel(raw, {1: 100})
    m.execute(1, 40, 7, 40)  # Ember: USDC (1) -> phUSD (7); USDC stays, phUSD joins the first empty slot
    assert m.collateral_asset_indexes == [1, 7, EMPTY_ASSET_INDEX]
    m.execute(7, 40, None, 0)  # deposit_funds drains phUSD
    m.venue_call(9, opens=True)  # the venue joins
    assert m.collateral_asset_indexes == [1, 9, EMPTY_ASSET_INDEX]
    m.venue_call(9, opens=False, closes=True)
    assert m.collateral_asset_indexes == [1, EMPTY_ASSET_INDEX, EMPTY_ASSET_INDEX]
