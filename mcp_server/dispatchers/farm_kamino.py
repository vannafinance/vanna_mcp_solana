"""`vanna_farm_kamino` — Kamino Lend farm writes (TOOLS_CATALOGUE.md §3.10).
Every action returns the schema-v1 write envelope. This dispatcher never
signs or broadcasts anything.
"""

from __future__ import annotations

from vanna_core.assets import assert_farm_listed

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

FarmKaminoAction = Literal["deposit", "redeem"]


@tool_safe
async def vanna_farm_kamino(
    action: FarmKaminoAction, authority: str, underlying: str, amount: str
) -> dict[str, Any]:
    """Build an unsigned Kamino Lend farm transaction (`margin_execute`).

    Args:
        action: "deposit" — move `amount` of `underlying` from the margin
            account's own collateral vault into Kamino, receiving the
            matching cToken as new collateral. The underlying must already
            be an *active collateral* position (deposit it first via
            `vanna_margin_trade.deposit`). "redeem" — the reverse: burn
            `amount` of the cToken (kSOL/kUSDC/...) for the underlying.
        authority: Base58 wallet address that owns the margin account.
        underlying: The underlying asset symbol (e.g. "SOL", "USDC") — not
            the receipt symbol.
        amount: Human-readable amount — of the underlying for "deposit", of
            the receipt token for "redeem".

    Returns: the schema-v1 write envelope.
    On error: {"error": "contract_error" | "invalid_input" | "account_not_found"
        | "simulation_failed", "message": "..."}
    """
    ctx = get_context()
    farm = ctx.farm_kamino
    authority_pubkey = Pubkey.from_string(authority)

    if action == "deposit":
        assert_farm_listed(underlying)
        envelope = await farm.deposit(authority_pubkey, underlying, amount)
        return envelope.to_dict()
    if action == "redeem":
        envelope = await farm.redeem(authority_pubkey, underlying, amount)
        return envelope.to_dict()
    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_farm_kamino. Supported: deposit, redeem",
    }
