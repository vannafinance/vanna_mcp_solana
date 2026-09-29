"""`vanna_history` — event history, no indexer (TOOLS_CATALOGUE.md §3.12)."""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

HistoryAction = Literal["margin", "earn"]


@tool_safe
async def vanna_history(
    action: HistoryAction, authority: str, asset: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """Decoded on-chain event history (from `getSignaturesForAddress` +
    per-transaction log parsing — no indexer, so results are best-effort and
    bounded by `limit`; `complete: false` means older events may exist beyond
    what was scanned).

    Args:
        action: "margin" — deposit/withdraw/borrow/repay/liquidation events
            for this wallet's margin account, each with post-action health
            factor where the event reports one. "earn" — supply/redeem
            events for this wallet in one Earn pool (`asset` required).
        authority: Base58 wallet address.
        asset: Required for "earn" — which pool's history to read.
        limit: Max transactions to scan (default 50).

    On error: {"error": "contract_error" | "invalid_input", "message": "..."}
    """
    ctx = get_context()
    authority_pubkey = Pubkey.from_string(authority)

    if action == "margin":
        return await ctx.history.get_margin_history(authority_pubkey, limit)
    if action == "earn":
        if asset is None:
            return {"error": "invalid_input", "message": "'asset' is required for action=earn"}
        return await ctx.history.get_earn_history(authority_pubkey, asset, limit)
    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_history. Supported: margin, earn",
    }
