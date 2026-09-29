"""`vanna_fork` — dev-only faucet (TOOLS_CATALOGUE.md §3.15). Registered only
when `ENABLE_FORK_TOOLS=true` (see `mcp_server/main.py`); never in the
Copilot's allowlist even then.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

ForkAction = Literal["faucet"]


@tool_safe
async def vanna_fork(action: ForkAction, address: str, symbol: str | None = None) -> dict[str, Any]:
    """Fork-only dev faucet — a Surfpool cheatcode, not a real transaction.

    Args:
        action: "faucet" — top up `address` with `symbol` (or every
            registered asset, if omitted).
        address: Base58 wallet address to fund.
        symbol: One registered asset, or omit to fund every registry token.

    On error: {"error": "invalid_input" | "contract_error", "message": "..."}
    """
    if action != "faucet":
        return {"error": "invalid_input", "message": f"Unknown action {action!r} for vanna_fork. Supported: faucet"}
    ctx = get_context()
    return await ctx.fork.faucet(address, symbol)
