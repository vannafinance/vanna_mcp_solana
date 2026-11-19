"""Offline tests for the weighted health lines of the 2026-11 audit upgrade (`vanna-credit-layer` risk_engine.rs):
each collateral counts at min(bps x 1.1, 100%) of its value, debt in full; the borrow line (LTV) gates actions, the
liquidation line (threshold) is the health factor shown. Also the new instruction encodings (`tokens`, rule books)."""

from __future__ import annotations

import pytest
from solders.instruction import AccountMeta
from solders.pubkey import Pubkey

from vanna_core import constants, math_utils, risk
from vanna_core.assets import _EMPTY_ORACLE, DiscoveredAsset
from vanna_core.contracts.margin import preview_verdict, weighted_row
from vanna_core.margin_execute import _ix_data, rule_book_pda, validator_segment

NANO = 10**9
VALIDATOR = Pubkey.from_string("6fND3vhtstp486iE6rsSNVUox3kcjNNRsSLAN7ZPs7th")


def _asset(symbol: str, ltv: int, threshold: int, collateral_enabled: bool = True) -> DiscoveredAsset:
    return DiscoveredAsset(
        symbol=symbol, asset_index=0, config_address=str(Pubkey.default()), mint=str(Pubkey.default()),
        reserve=str(Pubkey.default()), decimals=6, token_program=str(Pubkey.default()),
        collateral_enabled=collateral_enabled, borrow_enabled=True, oracle=_EMPTY_ORACLE,
        max_collateral_per_margin=0, liquidation_bonus_bps=500, ltv_bps=ltv, liquidation_threshold_bps=threshold,
    )


# ---------------------------------------------------------------- the program's weight rule


@pytest.mark.parametrize("bps, weight", [(8000, 8800), (7000, 7700), (5500, 6050), (8500, 9350), (9500, 10_000), (0, 0), (9091, 10_000)])
def test_line_weight_is_bps_times_1_1_capped_at_100_percent(bps: int, weight: int) -> None:
    assert math_utils.line_weight(bps) == weight


def test_weigh_rounds_down_and_leaves_full_weight_untouched() -> None:
    assert math_utils.weigh(999, 8800) == 879  # 879.12 -> 879
    assert math_utils.weigh(12_345, 10_000) == 12_345


def test_asset_weights_follow_its_config_and_collateral_flag() -> None:
    sol = _asset("SOL", 7000, 8000)
    assert (sol.borrow_weight_bps, sol.liquidation_weight_bps) == (7700, 8800)
    disabled = _asset("X", 7000, 8000, collateral_enabled=False)
    assert disabled.borrow_weight_bps == 0  # counts nowhere on the borrow line
    assert disabled.liquidation_weight_bps == 8800


def test_weighted_health_matches_a_hand_computation() -> None:
    # 10 SOL at $100 (borrow 77%, liq 88%) + 500 USDC (88%, 93.5%) against 600 USDC of debt
    sol, usdc = _asset("SOL", 7000, 8000), _asset("USDC", 8000, 8500)
    rows = [(1000 * NANO, sol), (500 * NANO, usdc)]
    borrow = math_utils.calculate_weighted_health([(v, a.borrow_weight_bps) for v, a in rows], [600 * NANO])
    liq = math_utils.calculate_weighted_health([(v, a.liquidation_weight_bps) for v, a in rows], [600 * NANO])
    assert borrow.total_collateral_value == (770 + 440) * NANO
    assert liq.total_collateral_value == (880 + 467.5) * NANO
    assert float(borrow.health_factor_str) == pytest.approx(1210 / 600)
    assert float(liq.health_factor_str) == pytest.approx(1347.5 / 600)


def test_unweighted_helper_is_unchanged() -> None:
    h = math_utils.calculate_health([1000, 500], [600])
    assert h.total_collateral_value == 1500


# ---------------------------------------------------------------- previews (the program's borrow-line check)


def test_preview_allows_on_the_borrow_line_and_reports_both_lines() -> None:
    usdc = _asset("USDC", 8000, 8500)
    now = [weighted_row(usdc, 1000 * NANO)]
    # borrow 700 USDC more: kept as collateral at 88%. Borrow line (880 + 616) / 700 = 2.137
    after = [weighted_row(usdc, 1700 * NANO)]
    verdict = preview_verdict(now, after, [700 * NANO], [0])
    assert verdict["allowed"] is True
    assert float(verdict["projected_health_factor"]) == pytest.approx(1496 / 700)
    assert float(verdict["projected_liquidation_health_factor"]) == pytest.approx(1700 * 0.935 / 700)
    assert verdict["current_health_factor"] is None  # no debt before


def test_preview_refuses_what_only_full_value_collateral_would_pass() -> None:
    # 1000 of xStock-like collateral (55% LTV -> 60.5%) against 800 of debt: unweighted 1.25 passes, the program refuses
    stock = _asset("NVDAx", 5500, 6500)
    rows = [weighted_row(stock, 1000 * NANO)]
    verdict = preview_verdict(rows, rows, [800 * NANO])
    assert verdict["allowed"] is False
    assert "1.10" in verdict["reason"]
    assert float(verdict["projected_health_factor"]) == pytest.approx(605 / 800)


# ---------------------------------------------------------------- closed forms


def test_max_borrow_with_a_weight_lands_on_the_floor() -> None:
    c, d, f, w = 1210.0, 600.0, 1.15, 0.88
    x = risk.max_borrow_usd(c, d, f, w)
    assert (c + w * x) / (d + x) == pytest.approx(f)
    # bool forms keep their old meaning
    assert risk.max_borrow_usd(c, d, f, True) == pytest.approx((c - f * d) / (f - 1))
    assert risk.max_borrow_usd(c, d, f, False) == pytest.approx(c / f - d)


def test_max_withdraw_with_a_weight_lands_on_the_floor() -> None:
    c, d, f, w = 1210.0, 600.0, 1.15, 0.77
    x = risk.max_withdraw_usd(c, d, f, w)
    assert (c - w * x) / d == pytest.approx(f)
    assert risk.max_withdraw_usd(c, d, f, 0.0) == float("inf")  # an asset that weighs 0 can all leave
    assert risk.max_withdraw_usd(c, 0.0, f, w) == c


def test_size_lever_counts_deposit_and_received_asset_at_their_weight() -> None:
    lever = risk.size_lever(collateral_usd=0.0, debt_usd=0.0, equity_usd=1000.0, deposit_usd=1000.0, leverage=2.0,
                            floor=1.15, slippage_bps=0, deposit_weight=0.77, received_weight=0.77)
    assert lever.ok and lever.hf_after == pytest.approx((770 + 770) / 1000)
    top = risk.max_leverage(collateral_usd=0.0, debt_usd=0.0, equity_usd=1000.0, deposit_usd=1000.0, floor=1.15,
                            slippage_bps=0, deposit_weight=0.77, received_weight=0.77)
    b = (top - 1) * 1000
    assert (770 + 0.77 * b) / b == pytest.approx(1.15)


# ---------------------------------------------------------------- instruction encodings (2026-11 upgrade)


def test_margin_execute_data_ends_with_all_tokens() -> None:
    data = _ix_data(b"\x01\x02", 3)
    assert data[-1] == constants.ALL_TOKENS == 255
    assert data[-3:-1] == (3).to_bytes(2, "little")


class _Settings:
    vanna_validator_program_id = str(VALIDATOR)


class _Client:
    settings = _Settings()


@pytest.mark.parametrize("program", constants.RULE_BOOK_PROGRAMS)
def test_rule_book_programs_get_the_validator_and_their_rule_book(program: str) -> None:
    target = Pubkey.from_string(program)
    segment = validator_segment(_Client(), target)
    assert [m.pubkey for m in segment] == [VALIDATOR, rule_book_pda(VALIDATOR, target)]
    assert all(not m.is_signer and not m.is_writable for m in segment)


def test_rule_book_pda_matches_the_fork() -> None:
    kamino = Pubkey.from_string(constants.KLEND_PROGRAM)
    assert str(rule_book_pda(VALIDATOR, kamino)) == "AKpEHoSsCQHG8odrRVg8WRjiH5u1uJZCtRmyUmcMg66q"


def test_code_reviewed_programs_get_no_rule_book_and_explicit_accounts_win() -> None:
    jupiter = Pubkey.from_string(constants.JUPITER_V6_PROGRAM)
    assert validator_segment(_Client(), jupiter) == []
    book = Pubkey.new_unique()
    segment = validator_segment(_Client(), Pubkey.from_string(constants.KLEND_PROGRAM), [book])
    assert [m.pubkey for m in segment] == [VALIDATOR, book]
    assert isinstance(segment[0], AccountMeta)
