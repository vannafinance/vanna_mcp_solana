"""MCP tools for lender (Earn) operations: supply/redeem liquidity, pool stats.

All amounts in tool signatures are human-readable strings (e.g. "1.5"), not
raw lamports/base units. Every write tool returns an unsigned, simulated
transaction — it never signs or submits (see
VANNA_SOLANA_MCP_REQUIREMENTS.md Part A #3 for the signing-service handoff).
"""

from __future__ import annotations

from typing import Any

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe


def _tx_dict(tx: Any) -> dict[str, Any]:
    return {
        "unsigned_tx_b64": tx.unsigned_tx_b64,
        "fee_payer": tx.fee_payer,
        "instruction": tx.instruction_name,
        "program_id": tx.program_id,
        "simulation_success": tx.simulation_success,
        "simulation_error": tx.simulation_error,
        "units_consumed": tx.units_consumed,
    }


@tool_safe
async def vanna_get_pool_stats(asset: str) -> dict[str, Any]:
    """Live stats for a Vanna Solana lending reserve (Earn pool).

    Returns utilization, borrow/supply APY, and liquidity/borrow/asset totals,
    all as human-readable decimal strings.

    Args:
        asset: "SOL" or "USDC" (case-insensitive).

    On error: {"error": "contract_error" | "invalid_input", "message": "..."}
    """
    ctx = get_context()
    reserve = ctx.reserve(asset)
    stats = await reserve.get_pool_stats()
    return stats.model_dump()


@tool_safe
async def vanna_get_deposited_balance(owner: str, asset: str) -> dict[str, Any]:
    """A lender's v-token (share) balance in a reserve, human-readable.

    Args:
        owner: Base58 wallet address.
        asset: "SOL" or "USDC".
    """
    ctx = get_context()
    reserve = ctx.reserve(asset)
    balance = await reserve.get_deposited_balance(Pubkey.from_string(owner))
    return {"asset": reserve.symbol, "share_balance": balance}


@tool_safe
async def vanna_supply_liquidity(owner: str, asset: str, amount: str) -> dict[str, Any]:
    """Build an unsigned lender_supply transaction (deposit into Earn).

    NOTE: assumes the lender's own token account for `asset` already holds
    `amount` (SOL must already be wrapped to WSOL in that account). Prefix
    instructions (idempotent ATA create + SOL wrap) are not yet added here —
    see vanna_core/contracts/reserve.py module docstring.

    Args:
        owner: Base58 wallet address of the lender (fee payer + signer).
        asset: "SOL" or "USDC".
        amount: Human-readable amount to deposit, e.g. "1.5".

    Returns: {unsigned_tx_b64, fee_payer, instruction, simulation_success, ...}
    On error: {"error": "simulation_failed" | "invalid_input", "message": "..."}
    """
    ctx = get_context()
    reserve = ctx.reserve(asset)
    tx = await reserve.build_supply_tx(Pubkey.from_string(owner), amount)
    return _tx_dict(tx)


@tool_safe
async def vanna_redeem_liquidity(owner: str, asset: str, shares: str) -> dict[str, Any]:
    """Build an unsigned lender_redeem transaction (withdraw from Earn).

    Args:
        owner: Base58 wallet address of the lender (fee payer + signer).
        asset: "SOL" or "USDC".
        shares: Human-readable v-token amount to redeem, e.g. "1.5".
    """
    ctx = get_context()
    reserve = ctx.reserve(asset)
    tx = await reserve.build_redeem_tx(Pubkey.from_string(owner), shares)
    return _tx_dict(tx)


@tool_safe
async def vanna_refresh_reserve(fee_payer: str, asset: str) -> dict[str, Any]:
    """Build an unsigned public_refresh_reserve transaction (permissionless
    interest-accrual tick). Useful before a stats read that must reflect
    just-accrued interest.

    Args:
        fee_payer: Base58 address that will pay the transaction fee.
        asset: "SOL" or "USDC".
    """
    ctx = get_context()
    reserve = ctx.reserve(asset)
    tx = await reserve.build_refresh_tx(Pubkey.from_string(fee_payer))
    return _tx_dict(tx)
