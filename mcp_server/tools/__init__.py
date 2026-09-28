"""Vanna Solana MCP tool handlers.

Exports ALL_TOOLS — the flat list of all tool functions registered with the
MCP server in main.py.

Only lender_tools (Earn: supply/redeem/pool-stats) is implemented so far. See
VANNA_SOLANA_MCP_REQUIREMENTS.md Part B in the Backend-Solana repo for the
remaining modules (account, collateral, borrow, repay, liquidation, oracle) —
each has its PDAs/instructions/account fields already specified there; they
follow the same pattern as lender_tools.py + vanna_core/contracts/reserve.py.
"""

from __future__ import annotations

from mcp_server.tools.lender_tools import (
    vanna_get_deposited_balance,
    vanna_get_pool_stats,
    vanna_redeem_liquidity,
    vanna_refresh_reserve,
    vanna_supply_liquidity,
)

ALL_TOOLS = [
    vanna_get_pool_stats,
    vanna_get_deposited_balance,
    vanna_supply_liquidity,
    vanna_redeem_liquidity,
    vanna_refresh_reserve,
]
