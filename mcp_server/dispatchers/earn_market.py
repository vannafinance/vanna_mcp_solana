"""`vanna_earn_market` — read-only pool stats (TOOLS_CATALOGUE.md §3.6)."""

from __future__ import annotations

from typing import Any, Literal

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe
from mcp_server.read_cache import cached_read

EarnMarketAction = Literal["pool_stats"]


@tool_safe
async def vanna_earn_market(action: EarnMarketAction, asset: str) -> dict[str, Any]:
    """Read-only Earn (lending pool) market data.

    Args:
        action: "pool_stats" — utilization, borrow/supply APR, liquidity,
            caps, reserve status, and rate-curve coefficients, all projected
            to the current chain clock.
        asset: One of the registered assets (case-insensitive; "SOL" or
            "wSOL", "USDC", "USDT", "JitoSOL", "JupSOL").

    On error: {"error": "contract_error" | "invalid_input", "message": "..."}
    """
    if action != "pool_stats":
        return {
            "error": "invalid_input",
            "message": f"Unknown action {action!r} for vanna_earn_market. Supported: pool_stats",
        }
    ctx = get_context()
    reserve = ctx.reserve(asset)

    async def fetch() -> dict[str, Any]:
        return (await reserve.get_pool_stats()).model_dump()

    return await cached_read(f"earn:pool_stats:{asset.lower()}", fetch)
