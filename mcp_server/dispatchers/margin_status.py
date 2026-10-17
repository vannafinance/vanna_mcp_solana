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
    """Low-level margin reads, mirroring the program's own checks (the 1.10 liquidation line).

    For "how much can I safely borrow / withdraw" use vanna_risk capacity (the product's 1.15 floor and the pool); the
    max_* actions here are the program's looser limits, for reference only.

    Actions:
      collateral / debt — every position, USD-priced.
      can_borrow / can_withdraw — would `amount` of `asset` pass the program's checks.
      max_borrow / max_withdraw — the program's limit for `asset` (1.10, not the safe floor).
      liquidation_snapshot — is the account liquidatable now (read only; liquidating is refused).
    authority: the wallet (omit when logged in).
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
