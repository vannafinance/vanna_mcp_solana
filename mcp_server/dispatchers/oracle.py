"""`vanna_oracle` - oracle-facade price reads (TOOLS_CATALOGUE.md 3.1)."""

from __future__ import annotations

from typing import Any, Literal

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe
from mcp_server.read_cache import cached_read

OracleAction = Literal["price", "prices"]


@tool_safe
async def vanna_oracle(action: OracleAction, asset: str | None = None, assets: list[str] | None = None) -> dict[str, Any]:
    """Read-only USD prices, read through the same oracle facade the program uses.

    Each asset is priced from its own on-chain OracleConfig: a Kamino Scope chain while fresh,
    else a Pyth feed (optionally x a second feed) when newer, x the Kamino exchange rate for
    receipts. Every price carries the quality flags the program checks for borrowing.

    Args:
        action: "price" - USD price for one asset (`asset`) with `source`, `age_seconds`,
            `fresh`/`stale`, `twap_ok`, `confidence_ok`. "prices" - the same, batched over
            `assets` (an unknown or unpriceable symbol errors only its own entry).
        asset: Any registered asset symbol, including Kamino receipts (kSOL, ...) and the
            other on-chain assets (required for "price").
        assets: A list of symbols (required for "prices").

    On error: {"error": "contract_error" | "invalid_input" | "price_unavailable", "message": "..."}
    """
    ctx = get_context()
    if action == "price":
        if asset is None:
            return {"error": "invalid_input", "message": "'asset' is required for action=price"}
        return await cached_read(f"oracle:price:{asset.lower()}", lambda: ctx.oracle.get_price(asset))
    if action == "prices":
        if not assets:
            return {"error": "invalid_input", "message": "'assets' (non-empty list) is required for action=prices"}
        key = "oracle:prices:" + ",".join(sorted(a.lower() for a in assets))
        return await cached_read(key, lambda: ctx.oracle.get_prices(assets))
    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_oracle. Supported: price, prices",
    }
