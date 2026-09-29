"""`vanna_farm_overview` — read-only Kamino Lend farm data (TOOLS_CATALOGUE.md
§3.9). Single-asset Kamino Lend only — Kamino Liquidity (multi-asset LP) is
not supported and cannot be (CPI rejected by that program).
"""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe
from mcp_server.read_cache import cached_read

FarmOverviewAction = Literal["markets", "positions"]


@tool_safe
async def vanna_farm_overview(action: FarmOverviewAction, authority: str | None = None) -> dict[str, Any]:
    """Read-only Kamino Lend farm data.

    Args:
        action: "markets" — every Kamino receipt asset, its cToken exchange
            rate, and live Kamino main-market APY (null if Kamino's API is
            unreachable, never guessed). "positions" — this wallet's farmed
            (Kamino receipt) collateral positions, in underlying units.
        authority: Required for "positions".

    On error: {"error": "contract_error" | "invalid_input", "message": "..."}
    """
    ctx = get_context()
    if action == "markets":
        return await cached_read("farm:markets", ctx.farm.list_markets)
    if action == "positions":
        if authority is None:
            return {"error": "invalid_input", "message": "'authority' is required for action=positions"}
        return await ctx.farm.get_positions(Pubkey.from_string(authority))
    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_farm_overview. Supported: markets, positions",
    }
