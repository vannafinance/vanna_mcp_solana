"""`vanna_wallet` — wallet-level reads (TOOLS_CATALOGUE.md §3.13)."""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

WalletAction = Literal["summary", "balance", "token_metadata", "faucet"]


@tool_safe
async def vanna_wallet(action: WalletAction, authority: str, asset: str | None = None) -> dict[str, Any]:
    """Wallet balances and token metadata.

    Args:
        action: "summary" — native SOL, every registered asset's wallet
            balance and Earn (lender) position, and whether a margin account
            exists. "balance" — wallet balance for one `asset`. "token_metadata"
            — decimals/supply/token program for one `asset`. "faucet" — TEST FORK ONLY: tops up
            the wallet with test tokens (1,000 of `asset`, or of every product token when omitted;
            a cheatcode, not a transaction). Limited per wallet per day. Only the connected wallet
            can be funded. Disabled unless ENABLE_TEST_FAUCET is on.
        authority: Base58 wallet address.
        asset: Required for "balance"/"token_metadata"; optional for "faucet".

    On error: {"error": "contract_error" | "invalid_input", "message": "..."}
    """
    ctx = get_context()
    if action == "faucet":
        from mcp_server.safety import get_safety

        if not ctx.settings.enable_test_faucet:
            return {"error": "invalid_input", "message": "The test faucet is not enabled on this server (ENABLE_TEST_FAUCET)."}
        Pubkey.from_string(authority)
        limit = await get_safety().consume_faucet(authority)
        if not limit.ok:
            return limit.to_error()
        return await ctx.fork.faucet(authority, asset)
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
        "message": f"Unknown action {action!r} for vanna_wallet. Supported: summary, balance, token_metadata, faucet",
    }
