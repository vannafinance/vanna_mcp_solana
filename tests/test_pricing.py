"""Offline tests for vanna_core.pricing — the port of the program's oracle facade.
Hand-built Scope/Pyth account bytes; no RPC."""

from __future__ import annotations

import pytest

from vanna_core import pricing as pr
from vanna_core.pricing import (
    ALL_CHECKS, CONFIDENCE_OK, FRESH, TWAP_OK, AccountData, OracleSpec, Price, PriceUnavailableError,
)

U = pr.SCOPE_CHAIN_UNUSED
SCOPE = "ScopeAccount1111111111111111111111111111111"
PYTH_A = "PythAccountA111111111111111111111111111111"
PYTH_B = "PythAccountB111111111111111111111111111111"
NOW = 1_800_000_000


def scope_account(entries: dict[int, tuple[int, int, int]], owner: str = pr.SCOPE_PROGRAM_ID) -> AccountData:
    data = bytearray(pr.ORACLE_PRICES_LEN)
    data[:8] = pr.ORACLE_PRICES_DISCRIMINATOR
    for index, (value, exp, ts) in entries.items():
        at = 40 + 56 * index
        data[at : at + 8] = value.to_bytes(8, "little")
        data[at + 8 : at + 16] = exp.to_bytes(8, "little")
        data[at + 24 : at + 32] = ts.to_bytes(8, "little")
    return AccountData(owner, bytes(data))


def pyth_account(price: int, conf: int, exponent: int, publish_time: int, ema: int | None = None,
                 owner: str = pr.PYTH_RECEIVER_PROGRAM_ID, full: bool = True) -> AccountData:
    data = bytearray(134)
    data[:8] = pr.PRICE_UPDATE_V2_DISCRIMINATOR
    data[40] = 1 if full else 0
    data[73:81] = price.to_bytes(8, "little", signed=True)
    data[81:89] = conf.to_bytes(8, "little")
    data[89:93] = exponent.to_bytes(4, "little", signed=True)
    data[93:101] = publish_time.to_bytes(8, "little", signed=True)
    data[109:117] = (ema if ema is not None else price).to_bytes(8, "little", signed=True)
    return AccountData(owner, bytes(data))


def spec(scope=False, chain=(U, U, U, U), twap_chain=(U, U, U, U), pyth="", factor="", age=120, twap_bps=1000, conf_bps=200):
    return OracleSpec(
        scope_prices=SCOPE if scope else "", scope_chain=chain, scope_twap_chain=twap_chain,
        pyth_price=pyth, pyth_factor=factor, klend_reserve="", klend_program="",
        max_age_secs=age, max_twap_divergence_bps=twap_bps, max_confidence_bps=conf_bps,
    )


# --- Price -----------------------------------------------------------------

def test_price_new_trims_wide_mantissa_and_rejects_zero():
    p = Price.new(pr.I64_MAX * 100, -8)
    assert p.value <= pr.I64_MAX and p.exponent == -6
    with pytest.raises(PriceUnavailableError):
        Price.new(0, -8)


def test_price_checked_mul_and_value_of():
    sol = Price.new(120_00000000, -8)  # $120
    rate = Price.new(1_20000000, -8)  # 1.2
    jup = sol.checked_mul(rate)
    assert jup.to_decimal_str() == "144"
    # 1 SOL (9 decimals) at $120 = 120 USD = 120e9 nano-USD (collateral rounds down)
    assert sol.value_of(10**9, 9, False) == 120 * 10**9


def test_diverges_from_boundary():
    a = Price.new(100_00000000, -8)
    assert not a.diverges_from(Price.new(105_00000000, -8), 500)  # exactly 5% is allowed
    assert a.diverges_from(Price.new(105_00000001, -8), 500)
    assert a.diverges_from(Price.new(94_00000000, -8), 500)
    assert not a.diverges_from(a, 0)


# --- Scope -----------------------------------------------------------------

def test_scope_single_entry_and_chain_product_uses_oldest_timestamp():
    account = scope_account({3: (118_000_000, 6, NOW - 10), 210: (1_090_000, 6, NOW - 50)})
    price, ts = pr.read_scope_chain(account, (3, U, U, U))
    assert price.to_decimal_str() == "118" and ts == NOW - 10
    price, ts = pr.read_scope_chain(account, (210, 3, U, U))
    assert price.to_decimal_str() == "128.62"  # 1.09 stake rate x $118
    assert ts == NOW - 50


def test_scope_zero_entry_is_none_not_zero_price():
    account = scope_account({3: (0, 6, NOW)})
    assert pr.read_scope_chain(account, (3, U, U, U)) is None


def test_scope_rejects_wrong_owner_and_bad_chain():
    with pytest.raises(PriceUnavailableError):
        pr.read_scope_chain(scope_account({3: (1, 0, NOW)}, owner="Wrong11111111111111111111111111111111111111"), (3, U, U, U))
    with pytest.raises(PriceUnavailableError):
        pr.scope_chain_entries((U, U, U, U))
    with pytest.raises(PriceUnavailableError):
        pr.scope_chain_entries((3, U, 4, U))  # gap
    with pytest.raises(PriceUnavailableError):
        pr.scope_chain_entries((600, U, U, U))  # outside the table


# --- Pyth ------------------------------------------------------------------

def test_pyth_read_flags_confidence_and_requires_full_verification():
    q = pr.read_pyth(pyth_account(118_00000000, 100_000_000, -8, NOW), 200)  # conf 1.0 / 118 = 0.85% <= 2%
    assert q.confidence_ok and q.publish_time == NOW
    wide = pr.read_pyth(pyth_account(118_00000000, 5_00000000, -8, NOW), 200)  # 4.2% > 2%
    assert not wide.confidence_ok
    with pytest.raises(PriceUnavailableError):
        pr.read_pyth(pyth_account(118_00000000, 1, -8, NOW, full=False), 200)
    with pytest.raises(PriceUnavailableError):
        pr.read_pyth(pyth_account(118_00000000, 1, -8, NOW, owner="Wrong11111111111111111111111111111111111111"), 200)


# --- get_price: source selection and status ----------------------------------

def test_fresh_scope_wins_over_pyth():
    accounts = {
        SCOPE: scope_account({3: (118_000_000, 6, NOW - 5), 455: (118_000_000, 6, NOW - 5)}),
        PYTH_A: pyth_account(200_00000000, 1, -8, NOW),  # would be newer and different, but must be ignored
    }
    p = pr.get_price(spec(scope=True, chain=(3, U, U, U), twap_chain=(455, U, U, U), pyth=PYTH_A), "m", accounts, NOW)
    assert p.source == "scope" and p.price.to_decimal_str() == "118"
    assert p.status == ALL_CHECKS


def test_stale_scope_falls_back_to_newer_pyth():
    accounts = {
        SCOPE: scope_account({3: (118_000_000, 6, NOW - 10_000)}),
        PYTH_A: pyth_account(119_00000000, 1, -8, NOW - 3),
    }
    p = pr.get_price(spec(scope=True, chain=(3, U, U, U), pyth=PYTH_A, twap_bps=0), "m", accounts, NOW)
    assert p.source == "pyth" and p.price.to_decimal_str() == "119" and p.status & FRESH


def test_stale_scope_and_older_pyth_keeps_scope_but_not_fresh():
    accounts = {
        SCOPE: scope_account({3: (118_000_000, 6, NOW - 1_000)}),
        PYTH_A: pyth_account(119_00000000, 1, -8, NOW - 5_000),
    }
    p = pr.get_price(spec(scope=True, chain=(3, U, U, U), pyth=PYTH_A, twap_bps=0), "m", accounts, NOW)
    assert p.source == "scope" and not p.status & FRESH
    with pytest.raises(PriceUnavailableError, match="stale_price"):
        p.require(ALL_CHECKS)
    p.require(0)


def test_pyth_factor_product_uses_older_timestamp_and_both_confidences():
    accounts = {
        PYTH_A: pyth_account(1_20000000, 1, -8, NOW - 2),        # JUPSOL/SOL rate 1.2
        PYTH_B: pyth_account(118_00000000, 5_00000000, -8, NOW - 9),  # SOL/USD, wide confidence
    }
    p = pr.get_price(spec(pyth=PYTH_A, factor=PYTH_B, twap_bps=0), "m", accounts, NOW)
    assert p.source == "pyth_x_pyth" and p.price.to_decimal_str() == "141.6"
    assert p.timestamp == NOW - 9
    assert not p.status & CONFIDENCE_OK


def test_twap_divergence_flagged_only_when_enabled():
    accounts = {
        SCOPE: scope_account({3: (118_000_000, 6, NOW), 455: (100_000_000, 6, NOW)}),  # 18% apart
    }
    off = pr.get_price(spec(scope=True, chain=(3, U, U, U), twap_chain=(455, U, U, U), twap_bps=0), "m", accounts, NOW)
    assert off.status & TWAP_OK
    on = pr.get_price(spec(scope=True, chain=(3, U, U, U), twap_chain=(455, U, U, U), twap_bps=1000), "m", accounts, NOW)
    assert not on.status & TWAP_OK
    with pytest.raises(PriceUnavailableError, match="twap"):
        on.require(ALL_CHECKS)
    on.require(pr.LIQUIDATION_CHECKS)  # a TWAP alarm never blocks a liquidation


def test_missing_needed_account_and_unconfigured_fail_closed():
    with pytest.raises(PriceUnavailableError, match="not provided"):
        pr.get_price(spec(scope=True, chain=(3, U, U, U)), "m", {}, NOW)
    with pytest.raises(PriceUnavailableError, match="no oracle"):
        pr.get_price(spec(), "m", {}, NOW)


def test_scope_zero_entry_without_pyth_is_unavailable():
    accounts = {SCOPE: scope_account({3: (0, 6, NOW)})}
    with pytest.raises(PriceUnavailableError, match="no usable"):
        pr.get_price(spec(scope=True, chain=(3, U, U, U), twap_bps=0), "m", accounts, NOW)


def test_oracle_spec_accounts_lists_only_configured():
    assert spec(scope=True, chain=(3, U, U, U), pyth=PYTH_A, factor=PYTH_B).accounts() == [SCOPE, PYTH_A, PYTH_B]
    assert spec(pyth=PYTH_A).accounts() == [PYTH_A]
