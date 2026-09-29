"""`vanna_wallet` — wallet-level reads (TOOLS_CATALOGUE.md §3.13)."""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

WalletAction = Literal["summary", "balance", "token_metadata"]


@tool_safe
async def vanna_wallet(action: WalletAction, authority: str, asset: str | None = None) -> dict[str, Any]:
    """Wallet balances and token metadata.

    Args:
        action: "summary" — native SOL, every registered asset's wallet
            balance and Earn (lender) position, and whether a margin account
            exists. "balance" — wallet balance for one `asset`. "token_metadata"
            — decimals/supply/token program for one `asset`.
        authority: Base58 wallet address.
        asset: Required for "balance"/"token_metadata".

    On error: {"error": "contract_error" | "invalid_input", "message": "..."}
    """
    ctx = get_context()
    if action == "summary":
        return await ctx.wallet.get_summary(Pubkey.from_string(authority))
    if action == "balance":
        if asset is None:
            return {"error": "invalid_input", "message": "'asset' is required for action=balance"}
        return await ctx.wallet.get_balance(Pubkey.from_string(authority), asset)
    if action == "token_metadata":
        if asset is None:
            return {"error": "invalid_input", "message": "'asset' is required for action=token_metadata"}
        return await ctx.wallet.get_token_metadata(asset)
    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_wallet. Supported: summary, balance, token_metadata",
    }
