"""`vanna_earn_market` — read-only pool stats (TOOLS_CATALOGUE.md §3.6)."""

from __future__ import annotations

from typing import Any, Literal

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe
from mcp_server.read_cache import cached_read

EarnMarketAction = Literal["pool_stats", "rate_curve"]


@tool_safe
async def vanna_earn_market(action: EarnMarketAction, asset: str) -> dict[str, Any]:
    """Read-only Earn (lending pool) market data.

    Args:
        action: "pool_stats" — utilization, borrow/supply APR, liquidity,
            caps, reserve status, and rate-curve coefficients, all projected
            to the current chain clock.
        asset: A lend/borrow asset (case-insensitive; "SOL" or "wSOL",
            "USDC", "USDT").

    On error: {"error": "contract_error" | "invalid_input", "message": "..."}
    """
    if action not in ("pool_stats", "rate_curve"):
        return {
            "error": "invalid_input",
            "message": f"Unknown action {action!r} for vanna_earn_market. Supported: pool_stats, rate_curve",
        }
    ctx = get_context()
    reserve = ctx.reserve(asset)

    async def fetch() -> dict[str, Any]:
        return (await reserve.get_pool_stats()).model_dump()

    stats = await cached_read(f"earn:pool_stats:{asset.lower()}", fetch)
    if action == "pool_stats" or "error" in stats:
        return stats
    return _rate_curve(asset, stats)


def _rate_curve(asset: str, stats: dict[str, Any]) -> dict[str, Any]:
    """Borrow and supply APR at every 5% of utilization, evaluated with the program's own rate math on the reserve's
    on-chain coefficients (the curve the website's rate chart draws)."""
    from vanna_core import math_utils

    linear, jump, mult = (int(stats[k]) for k in ("linear_coeff_wad", "jump_coeff_wad", "rate_multiplier_wad"))
    factor = int(stats["reserve_factor_bps"])
    points = []
    for pct in range(0, 101, 5):
        util = math_utils.WAD * pct // 100
        borrow = math_utils.annual_rate_pct(math_utils.borrow_rate_per_second_wad(linear, jump, mult, util))
        points.append({
            "utilization_pct": pct,
            "borrow_apr_pct": str(borrow),
            "supply_apr_pct": str(math_utils.supply_apr_pct(borrow, util, factor)),
        })
    return {"asset": asset, "current_utilization_pct": stats["utilization_rate_pct"],
            "current_borrow_apr_pct": stats["borrow_apr_pct"], "current_supply_apr_pct": stats["supply_apr_pct"],
            "reserve_factor_bps": factor, "points": points,
            "note": "APR (not compounded). Rates move with utilization: they rise steeply as the pool nears full."}
