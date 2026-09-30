"""`vanna_account` — margin account lifecycle (TOOLS_CATALOGUE.md §3.3)."""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

AccountAction = Literal["resolve", "get", "health", "create", "close"]


@tool_safe
async def vanna_account(action: AccountAction, authority: str) -> dict[str, Any]:
    """Margin account lifecycle and health.

    Args:
        action: "resolve" — does this wallet have a margin account yet, and
            what's its address. "get" — status and the symbols of every
            active collateral/debt slot. "health" — per-position USD values,
            totals, health factor (or "no debt"), and liquidation eligibility.
            "create" — unsigned transaction that opens the margin account (needed
            before the first deposit). "close" — unsigned transaction that closes
            an empty margin account (no collateral or debt left) and returns its rent.
        authority: Base58 wallet address (the margin account's one owner).

    On error: {"error": "contract_error" | "invalid_input" | "account_not_found"
        | "unpricable_position" | "simulation_failed", "message": "..."}
    """
    ctx = get_context()
    authority_pubkey = Pubkey.from_string(authority)

    if action == "resolve":
        return await ctx.margin.resolve(authority_pubkey)
    if action == "get":
        return await ctx.margin.get(authority_pubkey)
    if action == "health":
        return await ctx.margin.health(authority_pubkey)
    if action == "create":
        envelope = await ctx.margin_trade.create_margin(authority_pubkey)
        return envelope.to_dict()
    if action == "close":
        envelope = await ctx.margin_trade.close_margin(authority_pubkey)
        return envelope.to_dict()
    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_account. Supported: resolve, get, health, create, close",
    }
