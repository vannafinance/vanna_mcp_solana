"""Fixed-point math for vanna_lending — ported 1:1 (formula and rounding) from
`vanna-credit-layer/programs/vanna_lending/src/math/{fixed_point,interest,
shares,health}.rs`, read directly from source on 2026-09-28. Every function
here mirrors one on-chain function; see each docstring's Rust source pointer.

All arithmetic uses Python `int` (arbitrary precision, matching Rust's
checked u128/u64) and `Decimal` only for final display-string formatting.
Money and health-factor values are never floats anywhere in this module.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

# Constants — verified against constants.rs.
WAD = 1_000_000_000_000_000_000  # 10**18
BALANCE_TO_BORROW_THRESHOLD_WAD = 1_100_000_000_000_000_000  # 1.10, hardcoded on-chain
USD_VALUE_DECIMALS = 9  # nano-USD internal precision (health.rs)
BASIS_POINTS = 10_000
SECONDS_PER_YEAR = 31_556_952  # average Gregorian year
MIN_INITIAL_SHARES = 1_000


# ---------------------------------------------------------------------------
# fixed_point.rs
# ---------------------------------------------------------------------------


def mul_div_floor(a: int, b: int, denom: int) -> int:
    """floor(a * b / denom)."""
    if denom == 0:
        raise ZeroDivisionError("mul_div_floor: denom is zero")
    return (a * b) // denom


def mul_div_ceil(a: int, b: int, denom: int) -> int:
    """ceil(a * b / denom)."""
    if denom == 0:
        raise ZeroDivisionError("mul_div_ceil: denom is zero")
    product = a * b
    floor = product // denom
    return floor if product % denom == 0 else floor + 1


def checked_pow10(exp: int) -> int:
    if exp > 38:
        raise OverflowError(f"checked_pow10: exponent too large: {exp}")
    return 10**exp


# ---------------------------------------------------------------------------
# interest.rs
# ---------------------------------------------------------------------------


def utilization_wad(accounted_liquidity_assets: int, total_borrow_assets: int) -> int:
    """utilization = total_borrows / (liquidity + total_borrows), WAD fraction. 0 when empty."""
    gross = accounted_liquidity_assets + total_borrow_assets
    if gross == 0:
        return 0
    return mul_div_floor(total_borrow_assets, WAD, gross)


def wad_pow(base: int, exp: int) -> int:
    """base^exp for a WAD-scaled base, by square-and-multiply, each step
    rounded half-up — byte-identical algorithm to the on-chain `wad_pow`."""
    half_wad = WAD // 2

    def wad_mul_round(a: int, b: int) -> int:
        return (a * b + half_wad) // WAD

    if base == 0:
        return WAD if exp == 0 else 0
    result = base if exp % 2 == 1 else WAD
    exp //= 2
    while exp > 0:
        base = wad_mul_round(base, base)
        if exp % 2 == 1:
            result = wad_mul_round(result, base)
        exp //= 2
    return result


def borrow_rate_per_second_wad(
    linear_coeff_wad: int, jump_coeff_wad: int, rate_multiplier_wad: int, util_wad: int
) -> int:
    """borrow_rate_per_second = rate_multiplier * (u*linear + u^32*linear + u^64*jump) / (365.2425d * WAD)."""
    linear_term = mul_div_floor(util_wad, linear_coeff_wad, WAD)
    steep_term = mul_div_floor(wad_pow(util_wad, 32), linear_coeff_wad, WAD)
    jump_term = mul_div_floor(wad_pow(util_wad, 64), jump_coeff_wad, WAD)
    polynomial = linear_term + steep_term + jump_term
    year_wad = SECONDS_PER_YEAR * WAD
    return mul_div_floor(rate_multiplier_wad, polynomial, year_wad)


class AccrualProjection:
    """Mirrors `math/interest.rs::AccrualResult` — the state a reserve would
    have after accruing interest to `now`, without writing it on-chain.
    Used for read-path "projected to chain time" display."""

    __slots__ = (
        "new_total_borrow_assets",
        "new_accrued_protocol_fees",
        "new_borrow_index_wad",
        "interest_accrued",
    )

    def __init__(
        self,
        new_total_borrow_assets: int,
        new_accrued_protocol_fees: int,
        new_borrow_index_wad: int,
        interest_accrued: int,
    ) -> None:
        self.new_total_borrow_assets = new_total_borrow_assets
        self.new_accrued_protocol_fees = new_accrued_protocol_fees
        self.new_borrow_index_wad = new_borrow_index_wad
        self.interest_accrued = interest_accrued


def project_accrual(
    *,
    accounted_liquidity_assets: int,
    total_borrow_assets: int,
    accrued_protocol_fees: int,
    borrow_index_wad: int,
    last_update_timestamp: int,
    reserve_factor_bps: int,
    linear_coeff_wad: int,
    jump_coeff_wad: int,
    rate_multiplier_wad: int,
    now_unix: int,
) -> AccrualProjection:
    """Read-only projection of `math/interest.rs::accrue` to `now_unix` (the
    chain clock's unix timestamp, not wall-clock time — see
    `client.get_chain_clock_unix_timestamp`). Never writes anything; a write
    tool that needs a freshly-accrued reserve still calls
    `public_refresh_reserve` on-chain."""
    elapsed = now_unix - last_update_timestamp
    if elapsed <= 0 or total_borrow_assets == 0:
        return AccrualProjection(total_borrow_assets, accrued_protocol_fees, borrow_index_wad, 0)

    util = utilization_wad(accounted_liquidity_assets, total_borrow_assets)
    rate_per_second = borrow_rate_per_second_wad(
        linear_coeff_wad, jump_coeff_wad, rate_multiplier_wad, util
    )
    growth_wad = rate_per_second * elapsed
    interest = mul_div_ceil(total_borrow_assets, growth_wad, WAD)
    protocol_fee = mul_div_floor(interest, reserve_factor_bps, BASIS_POINTS)
    new_total_borrow_assets = total_borrow_assets + interest
    new_accrued_protocol_fees = accrued_protocol_fees + protocol_fee
    new_borrow_index_wad = max(
        mul_div_floor(borrow_index_wad, new_total_borrow_assets, total_borrow_assets), WAD
    )
    return AccrualProjection(
        new_total_borrow_assets, new_accrued_protocol_fees, new_borrow_index_wad, interest
    )


def annual_rate_pct(rate_per_second_wad: int) -> Decimal:
    """Display-only: per-second WAD rate -> simple (non-compounded) annualized
    percentage, matching the app's own "APR, not APY" display convention."""
    annual_wad = rate_per_second_wad * SECONDS_PER_YEAR
    return (Decimal(annual_wad) / Decimal(WAD) * 100).quantize(
        Decimal("0.0001"), rounding=ROUND_DOWN
    )


def supply_apr_pct(borrow_apr_pct_value: Decimal, utilization_wad_value: int, reserve_factor_bps: int) -> Decimal:
    """supply_apr = borrow_apr * utilization * (1 - reserve_factor). Display convention
    (APR, not compounded APY) — matches the app's lib/solana/protocol-state.ts."""
    utilization_frac = Decimal(utilization_wad_value) / Decimal(WAD)
    reserve_factor_frac = Decimal(reserve_factor_bps) / Decimal(BASIS_POINTS)
    return (borrow_apr_pct_value * utilization_frac * (1 - reserve_factor_frac)).quantize(
        Decimal("0.0001"), rounding=ROUND_DOWN
    )


# ---------------------------------------------------------------------------
# shares.rs — the `_down`/`_up` suffix is the rounding direction; every
# conversion rounds against the user, so rounding can never drain the pool.
# ---------------------------------------------------------------------------


def lender_total_assets(
    accounted_liquidity_assets: int, total_borrow_assets: int, accrued_protocol_fees: int
) -> int:
    """lender_total_assets = liquidity + total_borrows - accrued_protocol_fees."""
    return accounted_liquidity_assets + total_borrow_assets - accrued_protocol_fees


def available_lender_cash(accounted_liquidity_assets: int, accrued_protocol_fees: int) -> int:
    """available_cash = max(liquidity - accrued_protocol_fees, 0)."""
    return max(accounted_liquidity_assets - accrued_protocol_fees, 0)


def assets_to_supply_shares_down(
    assets_received: int, total_share_supply: int, lender_total_assets_before: int
) -> int:
    """shares = floor(assets * total_supply_shares / lender_total_assets)  (1:1 for the first deposit)."""
    if total_share_supply == 0 or lender_total_assets_before == 0:
        return assets_received
    return mul_div_floor(assets_received, total_share_supply, lender_total_assets_before)


def supply_shares_to_assets_down(shares: int, total_share_supply: int, lender_total_assets_value: int) -> int:
    """assets = floor(shares * lender_total_assets / total_supply_shares)."""
    if total_share_supply == 0:
        return 0
    return mul_div_floor(shares, lender_total_assets_value, total_share_supply)


def assets_to_debt_shares_up(assets: int, total_borrow_shares: int, total_borrow_assets: int) -> int:
    """borrow_shares = ceil(assets * total_borrow_shares / total_borrows)  (1:1 for the first borrow)."""
    if total_borrow_shares == 0 or total_borrow_assets == 0:
        return assets
    return mul_div_ceil(assets, total_borrow_shares, total_borrow_assets)


def debt_shares_to_assets_up(borrow_shares: int, total_borrow_shares: int, total_borrow_assets: int) -> int:
    """debt = ceil(borrow_shares * total_borrows / total_borrow_shares)."""
    if total_borrow_shares == 0:
        return 0
    return mul_div_ceil(borrow_shares, total_borrow_assets, total_borrow_shares)


# ---------------------------------------------------------------------------
# health.rs
# ---------------------------------------------------------------------------


def normalize_token_value(
    token_amount: int, price: int, price_exponent: int, token_decimals: int, round_up: bool
) -> int:
    """USD value of a raw token amount, in nano-USD (USD_VALUE_DECIMALS). Round
    collateral down (round_up=False), debt up (round_up=True)."""
    if price <= 0:
        raise ValueError(f"invalid_price: price must be positive, got {price}")
    base = token_amount * price
    net_exp = price_exponent - token_decimals + USD_VALUE_DECIMALS
    if net_exp >= 0:
        return base * checked_pow10(net_exp)
    factor = checked_pow10(-net_exp)
    if round_up:
        return (base + factor - 1) // factor
    return base // factor


def value_to_token_amount(
    value: int, price: int, price_exponent: int, token_decimals: int, round_up: bool
) -> int:
    """Inverse of normalize_token_value — a USD value back into raw token units."""
    if price <= 0:
        raise ValueError(f"invalid_price: price must be positive, got {price}")
    net_exp = price_exponent - token_decimals + USD_VALUE_DECIMALS
    if net_exp < 0:
        numerator_factor, denom_factor = checked_pow10(-net_exp), 1
    else:
        numerator_factor, denom_factor = 1, checked_pow10(net_exp)
    denom = price * denom_factor
    return mul_div_ceil(value, numerator_factor, denom) if round_up else mul_div_floor(
        value, numerator_factor, denom
    )


class HealthSnapshot:
    """Account health, matching `math/health.rs::HealthSnapshot`.

    `health_factor_wad` is `None` when there is no debt (the on-chain sentinel
    is `u128::MAX`; report "no debt", never a floating-point infinity).
    """

    __slots__ = ("total_collateral_value", "total_debt_value", "health_factor_wad")

    def __init__(self, total_collateral_value: int, total_debt_value: int) -> None:
        self.total_collateral_value = total_collateral_value
        self.total_debt_value = total_debt_value
        self.health_factor_wad: int | None = (
            None if total_debt_value == 0 else mul_div_floor(total_collateral_value, WAD, total_debt_value)
        )

    @property
    def is_borrow_healthy(self) -> bool:
        """healthy = total_debt == 0 || health_factor > 1.10."""
        return self.total_debt_value == 0 or self.health_factor_wad > BALANCE_TO_BORROW_THRESHOLD_WAD

    @property
    def is_liquidatable(self) -> bool:
        """liquidatable = total_debt > 0 && health_factor <= 1.10 (equality is liquidatable)."""
        return self.total_debt_value > 0 and self.health_factor_wad <= BALANCE_TO_BORROW_THRESHOLD_WAD

    @property
    def health_factor_str(self) -> str | None:
        if self.health_factor_wad is None:
            return None
        return str(Decimal(self.health_factor_wad) / Decimal(WAD))


def calculate_health(collateral_values: list[int], debt_values: list[int]) -> HealthSnapshot:
    """health_factor = sum(collateral_usd) / sum(debt_usd). Borrowed funds stay
    in the margin account and count as collateral — the caller must pass
    every active position's already-valued USD amount, not just the one
    being touched by the current action."""
    return HealthSnapshot(sum(collateral_values), sum(debt_values))


# ---------------------------------------------------------------------------
# UI conversions — human-readable decimal strings, never used for on-chain
# amounts or in health math (those stay raw ints throughout).
# ---------------------------------------------------------------------------


def ui_amount(raw: int, decimals: int) -> str:
    """Convert a raw on-chain integer amount to a human-readable decimal string."""
    if raw < 0:
        raise ValueError(f"raw amount must be non-negative, got {raw}")
    scale = 10**decimals
    whole, frac = divmod(raw, scale)
    if frac == 0:
        return str(whole)
    frac_str = str(frac).rjust(decimals, "0").rstrip("0")
    return f"{whole}.{frac_str}" if frac_str else str(whole)


def base_units(amount: "Decimal | str | int", decimals: int) -> int:
    """Convert a human-readable amount to raw on-chain integer units."""
    dec = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if dec <= 0:
        raise ValueError("Enter an amount greater than zero")
    return int(dec * (10**decimals))


def usd_value_ui(nano_usd: int) -> str:
    """A nano-USD (USD_VALUE_DECIMALS = 9) raw value to a 2dp display string."""
    return str((Decimal(nano_usd) / Decimal(10**USD_VALUE_DECIMALS)).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    ))
