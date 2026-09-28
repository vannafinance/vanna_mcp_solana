"""Fixed-point / decimal math for vanna_lending — ported from Backend-Solana's
lib/solana/protocol.ts (uiAmount/baseUnits), lib/pool-stats.ts (APY/exchange
rate), and lib/margin-health.ts (health factor).

All arithmetic uses Python int/Decimal. Float is never used for on-chain
amounts or USD-valued health math.

DEVIATION FROM THE FRONTEND, INTENTIONAL: Backend-Solana's
lib/utils/borrow-rate.ts computes borrow APR from a *hardcoded* synthetic
kinked curve (RATE_MODEL_PARAMS: baseRate=2%, optimalUtil=80%, slope1=4%,
slope2=60%), NOT from the Reserve account's own base_rate_bps/slope1_bps/
slope2_bps/optimal_utilization_bps fields — its own comment calls this an
"approximate testnet target" pending real params. But those real per-reserve
fields already exist on-chain today (confirmed live: SOL/USDC reserves both
have non-trivial base_rate_bps/slope1_bps/slope2_bps/optimal_utilization_bps
set) — see idl account Reserve. Since the MCP is meant to read ground truth,
``borrow_apr_pct`` here computes the same two-slope shape but parameterized
from the reserve's own on-chain bps fields, not the frontend's placeholder
constants. If the frontend's numbers and this module's numbers ever diverge,
this module's is the one actually backed by on-chain state.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from vanna_core.types import MarginHealth

BPS = Decimal(10_000)


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


def utilization_rate_pct(
    liquidity_raw: int, borrowed_raw: int, fees_raw: int
) -> Decimal:
    """Utilization = borrowed / (liquidity + borrowed - fees), as a percentage."""
    total = liquidity_raw + borrowed_raw - fees_raw
    if total <= 0:
        return Decimal("0")
    return (Decimal(borrowed_raw) / Decimal(total) * 100).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )


def borrow_apr_pct(
    utilization_pct: Decimal,
    base_rate_bps: int,
    slope1_bps: int,
    slope2_bps: int,
    optimal_utilization_bps: int,
) -> Decimal:
    """Two-slope kinked borrow-rate curve, parameterized from the Reserve's own
    on-chain bps fields (not a hardcoded placeholder — see module docstring).
    """
    base = Decimal(base_rate_bps) / 100
    slope1 = Decimal(slope1_bps) / 100
    slope2 = Decimal(slope2_bps) / 100
    optimal = Decimal(optimal_utilization_bps) / 100
    util = min(Decimal("100"), max(Decimal("0"), utilization_pct))

    if optimal <= 0:
        return base
    if util <= optimal:
        return base + (util / optimal) * slope1
    overflow = (util - optimal) / (Decimal("100") - optimal) if optimal < 100 else Decimal("0")
    return base + slope1 + overflow * slope2


def supply_apr_pct(borrow_apr: Decimal, utilization_pct: Decimal) -> Decimal:
    """Supply APR = borrow APR x utilization (protocol takes no ongoing spread;
    see lib/pool-stats.ts comment — origination fee is the one-time protocol
    revenue, not an interest-rate cut)."""
    return (borrow_apr * (utilization_pct / 100)).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )


def exchange_rate(total_assets_raw: int, share_supply_raw: int) -> str:
    """vToken -> underlying exchange rate. "1" for a fresh/empty pool."""
    if total_assets_raw <= 0 or share_supply_raw <= 0:
        return "1"
    rate = Decimal(total_assets_raw) / Decimal(share_supply_raw)
    return str(rate.quantize(Decimal("0.0000001"), rounding=ROUND_DOWN))


def derive_margin_health(
    gross_collateral_usd: Decimal,
    effective_debt_usd: Decimal,
    total_borrowed_usd: Decimal,
    liquidation_threshold: Decimal = Decimal("1.1"),
) -> MarginHealth:
    """Health/solvency figures — ported 1:1 from lib/margin-health.ts.

    health_factor      = gross_collateral_usd / debt_usd   (999 sentinel when no debt)
    liquidatable when  = health_factor <= liquidation_threshold
    debt_limit         = gross_collateral_usd / liquidation_threshold
    net_available      = gross_collateral_usd - debt_usd
    """
    if effective_debt_usd > 0:
        health_factor = float(gross_collateral_usd / effective_debt_usd)
    elif gross_collateral_usd > 0:
        health_factor = 999.0  # HEALTH_FACTOR_INFINITY_SENTINEL
    else:
        health_factor = 0.0

    collateral_left = max(
        Decimal("0"), gross_collateral_usd - effective_debt_usd * liquidation_threshold
    )
    net_available = max(Decimal("0"), gross_collateral_usd - effective_debt_usd)
    total_value = net_available + total_borrowed_usd
    debt_limit = (
        gross_collateral_usd / liquidation_threshold if gross_collateral_usd > 0 else Decimal("0")
    )

    return MarginHealth(
        health_factor=health_factor,
        collateral_left_before_liquidation_usd=float(collateral_left),
        net_available_collateral_usd=float(net_available),
        debt_limit_usd=float(debt_limit),
        total_value_usd=float(total_value),
    )


def debt_from_shares(borrow_shares: int, total_borrow_shares: int, total_borrow_assets: int) -> int:
    """Raw debt amount from a DebtPosition's shares — mirrors lib/margin-utils.ts
    debtRaw() (round UP, since debt owed should never be under-reported)."""
    if total_borrow_shares == 0:
        return 0
    return (borrow_shares * total_borrow_assets + total_borrow_shares - 1) // total_borrow_shares
