"""`vanna_earn_position` — a lender's own Earn position (TOOLS_CATALOGUE.md §3.7)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

EarnPositionAction = Literal["deposited_balance", "earned_interest"]


@tool_safe
async def vanna_earn_position(action: EarnPositionAction, owner: str, asset: str) -> dict[str, Any]:
    """Read-only view of one lender's position in one Earn pool.

    Args:
        action: "deposited_balance" — v-token (share) balance and redeemable
            underlying, both human-readable. "earned_interest" — redeemable
            underlying minus net historical principal (supplied − redeemed,
            from `vanna_history`'s event log) — best-effort, bounded by that
            log's scan limit.
        owner: Base58 wallet address of the lender.
        asset: One of the registered assets (case-insensitive).

    On error: {"error": "contract_error" | "invalid_input", "message": "..."}
    """
    ctx = get_context()
    owner_pubkey = Pubkey.from_string(owner)
    if action == "deposited_balance":
        reserve = ctx.reserve(asset)
        return await reserve.get_deposited_balance(owner_pubkey)
    if action == "earned_interest":
        reserve = ctx.reserve(asset)
        balance, history = await asyncio.gather(
            reserve.get_deposited_balance(owner_pubkey),
            ctx.history.get_earn_history(owner_pubkey, asset),
        )
        supplied = sum((Decimal(e["assets"]) for e in history["events"] if e["event"] == "LiquiditySupplied"), Decimal(0))
        redeemed = sum((Decimal(e["assets"]) for e in history["events"] if e["event"] == "LiquidityRedeemed"), Decimal(0))
        net_principal = supplied - redeemed
        earned = Decimal(balance["redeemable_underlying"]) - net_principal
        return {
            "asset": ctx.normalize_symbol(asset),
            "redeemable_underlying": balance["redeemable_underlying"],
            "net_principal_contributed": str(net_principal),
            "earned_interest": str(earned),
            "complete": history["complete"],
        }
    return {
        "error": "invalid_input",
        "message": (
            f"Unknown action {action!r} for vanna_earn_position. "
            "Supported: deposited_balance, earned_interest"
        ),
    }
