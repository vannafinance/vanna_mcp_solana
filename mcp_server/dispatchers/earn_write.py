"""`vanna_earn_write` — Earn supply/redeem, write path (TOOLS_CATALOGUE.md §3.8).

Every action returns the schema-v1 write envelope (vanna_core/envelope.py):
an unsigned, simulated v0 transaction plus a simulation result. This
dispatcher never signs or broadcasts anything.
"""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

EarnWriteAction = Literal["supply", "redeem"]


@tool_safe
async def vanna_earn_write(
    action: EarnWriteAction,
    owner: str,
    asset: str,
    amount: str | None = None,
    shares: str | None = None,
) -> dict[str, Any]:
    """Build an unsigned Earn supply/redeem transaction.

    Args:
        action: "supply" — deposit `amount` of `asset` into the pool, minting
            shares. "redeem" — burn `shares` for the underlying plus accrued
            interest.
        owner: Base58 wallet address of the lender (fee payer + required signer).
        asset: One of the registered assets (case-insensitive).
        amount: Human-readable amount to supply, e.g. "1.5". Required for "supply".
        shares: Human-readable v-token amount to redeem, e.g. "1.5". Required for "redeem".

    Returns: the schema-v1 write envelope — see vanna_core/envelope.py.
    On error: {"error": "simulation_failed" | "invalid_input", "message": "..."}
    """
    ctx = get_context()
    reserve = ctx.reserve(asset)
    owner_pubkey = Pubkey.from_string(owner)

    if action == "supply":
        if amount is None:
            return {"error": "invalid_input", "message": "'amount' is required for action=supply"}
        from vanna_core.assets import find_asset

        is_native = find_asset(asset) is not None and find_asset(asset).is_native
        envelope = await reserve.build_supply_tx(owner_pubkey, amount, is_native=is_native)
        return envelope.to_dict()

    if action == "redeem":
        if shares is None:
            return {"error": "invalid_input", "message": "'shares' is required for action=redeem"}
        envelope = await reserve.build_redeem_tx(owner_pubkey, shares)
        return envelope.to_dict()

    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_earn_write. Supported: supply, redeem",
    }
