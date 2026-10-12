"""Basis / Perena vaults: the vault program's arithmetic, instruction encodings and the receipt pricing (offline)."""

from __future__ import annotations

import struct

import pytest
from solders.pubkey import Pubkey

from vanna_core.contracts.vaults import (
    BASIS,
    DEPOSIT_DISC,
    PERENA,
    WITHDRAW_DISC,
    BasisVault,
    Route,
    VaultContract,
    basis_call,
    deposit_swap_amount,
    perena_call,
    read_multiplier,
    share_value,
    token_price,
    withdraw_swap_estimate,
)
from vanna_core.phoenix import PhoenixExchange
from vanna_core.pricing import FRESH, AccountData
from vanna_core.vaults import VAULTS


def test_vault_lookup_accepts_ids_symbols_and_names():
    assert VaultContract.find("basis-eth").id == "basis-eth"
    assert VaultContract.find("vbETH").id == "basis-eth"
    assert VaultContract.find("ETH basis").id == "basis-eth"
    assert VaultContract.find("USD*").id == "perena-usdstar"
    assert VaultContract.find("junior").id == "perena-junior"
    with pytest.raises(ValueError):
        VaultContract.find("doge-basis")


def test_vault_math_matches_the_program():
    # Half at 1x (target margin 10,000 bps); all of it for the SOL vault (0 bps).
    assert deposit_swap_amount(10_000_000, 10_000) == 5_000_000
    assert deposit_swap_amount(10_000_000, 0) == 10_000_000
    # The virtual offset: an empty vault prices a share at $1.
    assert share_value(1_000_000, 0, 0) == 1_000_000
    assert share_value(2_000_000, 3_000_000, 2_000_000) == 2_000_000 * 4_000_000 // 3_000_000
    # USD per whole token from ticks x tick size x 10^lot decimals (a multiplier of 1).
    assert token_price(2_500, 1_000, 3) == 2_500 * 1_000 * 1_000
    assert withdraw_swap_estimate(True, 50, 100, 1_000, 0, 1, 6) == 500
    assert withdraw_swap_estimate(False, 0, 0, 0, 0, 1, 6) == 0


def test_read_multiplier_defaults_to_one():
    assert read_multiplier(bytes(82), 0) == 10**12


def test_perena_calls_encode_like_the_app():
    margin = Pubkey.new_unique()
    star, junior = VAULTS[5], VAULTS[6]
    dep = perena_call(star, "deposit", margin, 5_000_000)
    assert dep.program_id == PERENA
    assert bytes(dep.data) == bytes([247, 103, 46, 184, 88, 188, 56, 46]) + struct.pack("<Q", 5_000_000)
    assert dep.accounts[0].pubkey == margin and not dep.accounts[0].is_writable
    jdep = perena_call(junior, "deposit", margin, 1)
    assert bytes(jdep.data) == bytes([231, 224, 46, 207, 175, 45, 77, 148, 0]) + struct.pack("<Q", 1)
    assert jdep.accounts[0].is_writable  # the tranche program writes the margin's account
    jwd = perena_call(junior, "withdraw", margin, 1)
    assert len(jwd.accounts) == len(jdep.accounts) - 2  # no fee accounts on a tranche withdrawal


def _vault(spot: bool) -> BasisVault:
    k = Pubkey.new_unique
    return BasisVault(
        address=k(), spot=spot, asset_mint=k(), asset_decimals=8, asset_token_program=k(), share_mint=k(), authority=k(),
        usdc_account=k(), phoenix_account=k(), asset_account=k() if spot else None, trader_account=k(), asset_id=7,
        orderbook=k(), spline_collection=k(), base_lot_decimals=3, tick_size=10, max_position_usd=0, max_deposit_usd=0,
        target_margin_bps=10_000, deposits_open=True, usdc_mint=k(), phoenix_mint=k(), paused=False,
    )


def _exchange() -> PhoenixExchange:
    k = Pubkey.new_unique
    return PhoenixExchange(canonical_mint=k(), global_vault=k(), perp_asset_map=k(), trader_index=(k(),),
                           active_buffer=(k(), k()), withdraw_queue=k(), ember_state=k(), ember_vault=k())


def test_basis_deposit_and_withdraw_encode_the_route_and_index_counts():
    from solders.instruction import AccountMeta

    vault, ex, user = _vault(True), _exchange(), Pubkey.new_unique()
    route = Route(metas=[AccountMeta(Pubkey.new_unique(), False, True)] * 3, data=b"\x01\x02", lookup_tables=[], in_amount=1, out_amount=1)
    dep = basis_call(vault, ex, "deposit", user, False, 10_000_000, 9_000_000, route)
    assert dep.program_id == BASIS
    assert bytes(dep.data) == DEPOSIT_DISC + bytes([1, 2]) + struct.pack("<QQ", 10_000_000, 9_000_000) + bytes([3]) + struct.pack("<I", 2) + b"\x01\x02"
    # user, 24 core accounts, shares, usdc, swap program, wsol, 3 programs, then the index accounts and the route.
    assert len(dep.accounts) == 1 + 24 + 2 + 2 + 3 + 3 + 3
    assert not dep.accounts[0].is_signer  # the margin signs through margin_execute
    wd = basis_call(vault, ex, "withdraw", user, False, 5, 4, None)
    assert bytes(wd.data) == WITHDRAW_DISC + bytes([1, 2]) + struct.pack("<QQ", 5, 4) + bytes([0])
    with pytest.raises(Exception):
        basis_call(vault, ex, "deposit", user, False, 1, 0, None)  # a deposit always swaps


def test_perena_price_reads_the_nav_and_its_age():
    from vanna_core.pricing import OracleSpec
    from vanna_core.vault_pricing import perena_price

    star = VAULTS[5]
    data = bytearray(star.price_at + 16)
    data[star.price_at : star.price_at + 8] = struct.pack("<Q", 1_101_795)
    data[star.price_at + 8 : star.price_at + 16] = struct.pack("<q", 1_000)
    spec = OracleSpec(scope_prices="", scope_chain=(0xFFFF,) * 4, scope_twap_chain=(0xFFFF,) * 4, pyth_price="",
                      pyth_factor="", klend_reserve=star.account, klend_program=str(PERENA), max_age_secs=3_600,
                      max_twap_divergence_bps=0, max_confidence_bps=0)
    assert spec.uses_vault_receipt and not spec.uses_klend
    accounts = {star.account: AccountData(owner=str(PERENA), data=bytes(data))}
    fresh = perena_price(spec, star.mint, accounts, now=2_000)
    assert fresh.price.value == 1_101_795 and fresh.price.exponent == -6 and fresh.status & FRESH
    assert not perena_price(spec, star.mint, accounts, now=10_000).status & FRESH


def test_funding_stats_annualise_hourly_rates_per_day_like_the_app():
    from vanna_core.contracts.vaults import HOURS_PER_YEAR, funding_stats

    day = 86_400_000
    rates = [(0, 0.001), (3_600_000, 0.003), (day, -0.002)]
    stats = funding_stats(rates)
    first, second = 0.002 * HOURS_PER_YEAR, -0.002 * HOURS_PER_YEAR
    assert stats["funding_apr_7d_pct"] == pytest.approx((first * 2 + second * 1) / 3)
    assert stats["negative_days_30"] == 1 and stats["days_30"] == 2


def test_validator_segment_names_each_priced_market():
    from vanna_core.phoenix import PHOENIX_EXCHANGE, MarketBook, MarketBookEntry, validator_accounts
    from vanna_core.pricing import OracleSpec

    k = Pubkey.new_unique
    price = k()
    spec = OracleSpec(scope_prices="", scope_chain=(0xFFFF,) * 4, scope_twap_chain=(0xFFFF,) * 4, pyth_price=str(price),
                      pyth_factor="", klend_reserve="", klend_program="", max_age_secs=60, max_twap_divergence_bps=0,
                      max_confidence_bps=0)
    market = k()
    book = MarketBook(address=k(), collateral_mint=k(), collateral_decimals=6, collateral_price=spec,
                      markets=(MarketBookEntry(market=market, asset_id=3, max_leverage_bps=0, trading_enabled=True, index_price=spec),))
    assert validator_accounts(book) == [book.address, PHOENIX_EXCHANGE]
    assert validator_accounts(book, [3, 3]) == [book.address, PHOENIX_EXCHANGE, market, price]
    with pytest.raises(Exception):
        validator_accounts(book, [9])


def test_app_apy_keeps_each_figure_with_its_source():
    from vanna_core.contracts.vaults import app_apy_fields

    estimate = app_apy_fields({"apy7": 12.5, "apy7Source": "deposit-event", "apy7BaselineAt": 1_759_276_800_000,
                               "apy30": None, "apyHistoryStatus": "available"})
    assert estimate["apy_7d_pct"] == 12.5 and estimate["apy_7d_source"] == "deposit_event_estimate"
    assert estimate["apy_7d_baseline"] == "2025-10-01"
    assert estimate["apy_30d_pct"] is None and estimate["apy_30d_source"] is None
    history = app_apy_fields({"apy7": 4.0, "apy7Source": "history", "apyHistoryStatus": "available"})
    assert history["apy_7d_source"] == "history" and history["apy_7d_baseline"] is None
    # An app that predates the source field only had share-price history.
    assert app_apy_fields({"apy7": 3.0})["apy_7d_source"] == "history"
    assert app_apy_fields({"apyHistoryStatus": "collecting"})["apy_history_status"] == "collecting"
    launch = app_apy_fields({"apy7": None, "returnSinceLaunchPct": -1.571800000000001, "sinceLaunchDays": 5.0263657,
                             "launchedAt": 1_760_140_800_000})
    assert launch["return_since_launch_pct"] == -1.5718 and launch["since_launch_days"] == 5.03
    assert launch["launched_at"] == "2025-10-11"
    gone = app_apy_fields({"returnSinceLaunchPct": None, "sinceLaunchDays": 2.0})
    assert gone["return_since_launch_pct"] is None and gone["since_launch_days"] is None
    # The old annualised field is no longer read.
    assert "apy_since_launch_pct" not in app_apy_fields({"apySinceLaunch": 8.25})


async def test_a_failed_basis_price_falls_back_to_the_last_good_one_flagged_stale(monkeypatch):
    from types import SimpleNamespace

    from vanna_core import oracle_reader, vault_pricing
    from vanna_core.pricing import ALL_CHECKS, OraclePrice, Price, PriceUnavailableError

    basis = VAULTS[1]
    asset = SimpleNamespace(mint=basis.mint, symbol=basis.symbol, oracle=SimpleNamespace(uses_vault_receipt=True, accounts=lambda: []))
    good = OraclePrice(price=Price.new(1_020_000_000, -9), receipt_rate=None, timestamp=1, status=ALL_CHECKS, source="receipt")
    outcomes = [good, PriceUnavailableError("429 from the fork")]

    async def fake_price(_client, _vault):
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    class Client:
        async def get_multiple_accounts_raw(self, keys):
            return {}

        async def get_chain_clock_unix_timestamp(self):
            return 100

    monkeypatch.setattr(vault_pricing, "basis_price", fake_price)
    monkeypatch.setattr(vault_pricing, "is_basis", lambda a: True)
    oracle_reader.clear_last_basis_prices()
    reader = oracle_reader.OracleReader.__new__(oracle_reader.OracleReader)
    reader._client = Client()
    first = await reader.view([asset])
    assert first.vault_prices[basis.mint] is good
    second = await reader.view([asset])
    stale = second.vault_prices[basis.mint]
    assert stale.price == good.price and not stale.status & FRESH
    with pytest.raises(PriceUnavailableError):
        stale.require(ALL_CHECKS)  # a health-checked write still refuses a stale share price
    oracle_reader.clear_last_basis_prices()


def test_routed_sol_basis_health_comes_from_the_collateral_withdrawn_log():
    import base64

    from vanna_core.contracts.vaults import _routed_health
    from vanna_core.layouts import EVENT_LAYOUTS, event_discriminator

    def event(wad: int) -> str:
        body = EVENT_LAYOUTS["CollateralWithdrawn"].build({
            "margin_account": [0] * 32, "mint": [0] * 32, "amount": 10_000_000, "new_collateral_amount": 0,
            "borrow_health_factor_wad": wad, "event_sequence": 1, "timestamp": 0,
        })
        return "Program data: " + base64.b64encode(event_discriminator("CollateralWithdrawn") + body).decode()

    other = "Program data: " + base64.b64encode(b"\x01" * 40).decode()
    assert _routed_health(["Program log: x", other, event(1_234_500_000_000_000_000)]) == "1.2345"
    assert _routed_health([event(2**127)]) is None  # no debt: the program reports "infinite"
    assert _routed_health(["Program log: nothing"]) is None
