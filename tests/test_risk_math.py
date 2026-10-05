"""Offline tests for vanna_core/risk.py — the Copilot's sizing and risk rules, ported 1:1."""

from __future__ import annotations

import math

import pytest

from vanna_core import risk


# ---------------------------------------------------------------- amounts


@pytest.mark.parametrize("raw, expected", [("100", "100"), ("0.5", "0.5"), (".5", "0.5"), ("1,000.50", "1000.5"),
                                           ("1,000", "1000"), ("0.000001", "0.000001"), (12.5, "12.5"), ("+3", "3")])
def test_parse_amount_accepts_plain_positive_decimals(raw, expected) -> None:
    value, issue = risk.parse_amount(raw)
    assert issue is None and value == expected


@pytest.mark.parametrize(
    "raw",
    ["-5", "−5", "0", "0.0", "", "   ", "abc", "NaN", "Infinity", "-Infinity", "1e-3", "1e12", "1,5", "1.000,5", "1,0,0",
     "1e1000", "9999999999999", "0.0000000000001"],
)
def test_parse_amount_refuses_what_it_cannot_read_exactly(raw: str) -> None:
    value, issue = risk.parse_amount(raw)
    assert value is None and issue is not None and issue.message


def test_parse_amount_never_rounds_to_token_precision() -> None:
    assert risk.parse_amount("1.1234567", "USDC")[0] is None  # USDC has 6 decimals
    assert "6 decimal" in risk.parse_amount("1.1234567", "USDC")[1].message
    assert risk.parse_amount("1.123456", "USDC")[0] == "1.123456"
    assert risk.parse_amount("0.123456789", "SOL")[0] == "0.123456789"
    assert risk.parse_amount("0.1234567891", "SOL")[0] is None


def test_lone_comma_is_ambiguous_not_guessed() -> None:
    assert risk.parse_amount("1,5")[1].kind == "ambiguous"


def test_floor_and_ceil_decimals() -> None:
    assert risk.floor_decimals(1.2399999, 2) == "1.23"
    assert risk.floor_decimals(0.0, 6) == "0" and risk.floor_decimals(-1, 6) == "0" and risk.floor_decimals(float("nan"), 6) == "0"
    assert risk.floor_decimals(5, 9) == "5"
    assert risk.ceil_units(1.0000001, "USDC") == "1.000001"
    assert risk.ceil_units(2.0, "SOL") == "2"
    assert risk.usd_to_units(150.0, 100.0, "SOL") == "1.5" and risk.usd_to_units(10, 0, "SOL") == "0"


def test_num_reads_only_finite_numbers() -> None:
    assert risk.num("1.5") == 1.5 and risk.num(2) == 2.0 and risk.num(" 3 ") == 3.0
    for bad in (None, "", "x", float("nan"), float("inf"), True, {}):
        assert risk.num(bad) is None


# ---------------------------------------------------------------- borrow / withdraw sizing


def test_max_borrow_keeps_health_at_the_floor() -> None:
    c, d, floor = 1000.0, 0.0, 1.15
    b = risk.max_borrow_usd(c, d, floor)
    assert math.isclose((c + b) / (d + b), floor, rel_tol=1e-9)  # borrowed funds count as collateral
    assert risk.max_borrow_usd(100, 100, 1.15) == 0.0  # no headroom
    assert risk.max_borrow_usd(1000, 0, 1.15, borrowed_is_collateral=False) == pytest.approx(1000 / 1.15)


def test_max_withdraw_and_hf() -> None:
    assert risk.max_withdraw_usd(1000, 0, 1.15) == 1000  # no debt: everything may leave
    assert risk.max_withdraw_usd(1000, 500, 1.15) == pytest.approx(425)
    assert risk.max_withdraw_usd(100, 500, 1.15) == 0.0
    assert risk.hf_after(300, 200) == 1.5 and risk.hf_after(300, 0) is None


# ---------------------------------------------------------------- leverage


def test_size_lever_holds_the_floor_even_at_the_worst_swap_price() -> None:
    # 1000 USD of equity, no debt: 2x borrows 1000; worst case swaps at -1%.
    s = risk.size_lever(collateral_usd=1000, debt_usd=0, equity_usd=1000, deposit_usd=0, leverage=2, floor=1.15)
    assert s.ok and s.borrow_usd == 1000
    assert s.hf_after == pytest.approx((1000 + 1000 * 0.99) / 1000)
    assert s.hf_after >= 1.15


def test_size_lever_refuses_above_the_floor_and_offers_a_safe_maximum() -> None:
    s = risk.size_lever(collateral_usd=1000, debt_usd=0, equity_usd=1000, deposit_usd=0, leverage=9, floor=1.15)
    assert not s.ok and "below" in s.reason
    assert s.max_leverage is not None
    # The offered maximum must itself be safe.
    again = risk.size_lever(collateral_usd=1000, debt_usd=0, equity_usd=1000, deposit_usd=0, leverage=s.max_leverage, floor=1.15)
    assert again.ok


def test_max_leverage_algebra_matches_a_direct_check() -> None:
    top = risk.max_leverage(collateral_usd=500, debt_usd=100, equity_usd=500, deposit_usd=0, floor=1.15, slippage_bps=100)
    borrow = (top - 1) * 500
    worst = (500 + borrow * 0.99) / (100 + borrow)
    assert worst == pytest.approx(1.15, rel=1e-9)
    assert risk.max_leverage(collateral_usd=100, debt_usd=100, equity_usd=100, deposit_usd=0, floor=1.15, slippage_bps=100) == 1.0
    assert risk.max_leverage(collateral_usd=0, debt_usd=0, equity_usd=0, deposit_usd=0, floor=1.15, slippage_bps=100) == 1.0


def test_size_lever_with_a_fresh_deposit_counts_it_as_collateral() -> None:
    with_deposit = risk.size_lever(collateral_usd=0, debt_usd=0, equity_usd=100, deposit_usd=100, leverage=3, floor=1.15)
    assert with_deposit.ok and with_deposit.borrow_usd == 200
    assert not risk.size_lever(collateral_usd=0, debt_usd=0, equity_usd=0, deposit_usd=0, leverage=2, floor=1.15).ok


# ---------------------------------------------------------------- gate


def test_risk_gate_only_gates_actions_that_can_lower_health() -> None:
    # Adding collateral / repaying is always allowed, even from an unhealthy account: that is how a user gets out.
    assert risk.risk_gate(increases_risk=False, hf_after_value=1.01, floor=1.15).decision == "allow"
    assert risk.risk_gate(increases_risk=False, hf_after_value=risk.UNKNOWN, floor=1.15).decision == "allow"


def test_risk_gate_blocks_below_floor_flags_thin_and_fails_closed_on_unknown() -> None:
    blocked = risk.risk_gate(increases_risk=True, hf_after_value=1.12, floor=1.15)
    assert blocked.decision == "block" and "1.12" in blocked.reasons[0] and "1.10" in blocked.reasons[0]
    assert risk.risk_gate(increases_risk=True, hf_after_value=1.20, floor=1.15).decision == "needs_confirmation"
    assert risk.risk_gate(increases_risk=True, hf_after_value=2.0, floor=1.15).decision == "allow"
    assert risk.risk_gate(increases_risk=True, hf_after_value=None, floor=1.15).decision == "allow"  # no debt afterwards
    unknown = risk.risk_gate(increases_risk=True, hf_after_value=risk.UNKNOWN, floor=1.15)
    assert unknown.decision == "block"


def test_risk_gate_boundaries() -> None:
    assert risk.risk_gate(increases_risk=True, hf_after_value=1.15, floor=1.15).decision == "needs_confirmation"
    assert risk.risk_gate(increases_risk=True, hf_after_value=1.25, floor=1.15).decision == "allow"
    assert risk.risk_gate(increases_risk=True, hf_after_value=1.1499999, floor=1.15).decision == "block"
