"""`vanna_margin_status` — collateral/debt reads and pre-flight gates
(TOOLS_CATALOGUE.md §3.4). All read-only; `liquidation_snapshot` is an
eligibility read, never an execution path — liquidation is never exposed as
a Copilot-facing write action anywhere in this server.
"""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

MarginStatusAction = Literal[
    "collateral", "debt", "can_borrow", "can_withdraw", "max_borrow", "max_withdraw", "liquidation_snapshot"
]


@tool_safe
async def vanna_margin_status(
    action: MarginStatusAction,
    authority: str,
    asset: str | None = None,
    amount: str | None = None,
) -> dict[str, Any]:
    """Read-only margin account position and risk data.

    Args:
        action: "collateral" — all open collateral positions, USD-priced.
            "debt" — all outstanding borrows, USD-priced. "can_borrow" — would
            borrowing `amount` of `asset` stay within the 1.10 health-factor
            threshold and pass every other gate (operating mode, reserve
            status, caps, slot limit)? Fails closed. "can_withdraw" — same
            question for withdrawing `amount` of `asset`. "max_borrow" /
            "max_withdraw" — the largest `asset` amount that still passes that
            same check (closed-form headroom, then verified); use these for
            sizing so "max" in chat equals what the write will accept.
            "liquidation_snapshot"
            — is this account liquidatable (debt > 0, health factor <= 1.10,
            fresh prices)? Liquidation is whole-account: a liquidator repays
            every debt in full and receives every collateral balance (no close
            factor, no bonus); `estimated_reward_usd` is collateral minus debt.
            Eligibility read only; no execution path here.
        authority: Base58 wallet address (the margin account's owner).
        asset: Required for "can_borrow"/"can_withdraw"/"max_borrow"/"max_withdraw".
        amount: Human-readable amount, required for "can_borrow"/"can_withdraw".

    On error: {"error": "contract_error" | "invalid_input" | "account_not_found"
        | "unpricable_position", "message": "..."}
    """
    ctx = get_context()
    authority_pubkey = Pubkey.from_string(authority)

    if action == "collateral":
        return await ctx.margin.list_collateral(authority_pubkey)
    if action == "debt":
        return await ctx.margin.list_debt(authority_pubkey)
    if action == "liquidation_snapshot":
        return await ctx.margin.liquidation_snapshot(authority_pubkey)
    if action in ("max_borrow", "max_withdraw"):
        if asset is None:
            return {"error": "invalid_input", "message": f"'asset' is required for action={action}"}
        if action == "max_borrow":
            return await ctx.margin.max_borrow(authority_pubkey, asset)
        return await ctx.margin.max_withdraw(authority_pubkey, asset)
    if action == "can_borrow":
        if asset is None or amount is None:
            return {"error": "invalid_input", "message": "'asset' and 'amount' are required for action=can_borrow"}
        return await ctx.margin.preview_borrow(authority_pubkey, asset, amount)
    if action == "can_withdraw":
        if asset is None or amount is None:
            return {"error": "invalid_input", "message": "'asset' and 'amount' are required for action=can_withdraw"}
        return await ctx.margin.preview_withdraw(authority_pubkey, asset, amount)
    return {
        "error": "invalid_input",
        "message": (
            f"Unknown action {action!r} for vanna_margin_status. "
            "Supported: collateral, debt, can_borrow, can_withdraw, max_borrow, max_withdraw, liquidation_snapshot"
        ),
    }
