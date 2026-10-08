"""`vanna_swap` — Jupiter and Titan swaps, margin-account and wallet (TOOLS_CATALOGUE.md
§3.11). Plain registered assets are swappable; Kamino receipts are not
(they have no market - redeem them through `vanna_farm_kamino` instead). Every write action returns the schema-v1
envelope; this dispatcher never signs or broadcasts anything.
"""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

SwapAction = Literal["quote", "can_swap", "margin_swap", "wallet_swap"]
SwapProvider = Literal["jupiter", "titan"]


@tool_safe
async def vanna_swap(
    action: SwapAction,
    from_asset: str,
    to_asset: str,
    amount: str,
    authority: str | None = None,
    slippage_bps: int = 100,
    provider: SwapProvider | None = None,
    mode: Literal["wallet", "margin"] = "wallet",
) -> dict[str, Any]:
    """Quote or execute a swap through Jupiter or Titan.

    Two providers, like the app's Spot page. "quote" returns both providers' quotes (`providers`, best first, each with
    its output, minimum received and route) and the better one at the top level. If the user did not name a provider,
    show them both quotes and ask which to use; then pass it as `provider`. With `provider` omitted, the builds use
    whichever quote is better at build time. Titan quotes only when the server has a Titan key and a wallet is given.

    Args:
        action: "quote" — read-only ExactIn quote. "can_swap" — would a
            margin-account swap of `amount` `from_asset` into `to_asset`
            stay within the 1.10 health-factor threshold (`authority`
            required; N/A for `wallet_swap`, which never touches a margin
            account). "margin_swap" — swap collateral inside the margin
            account (`from_asset` must already be active collateral).
            "wallet_swap" — a plain wallet-to-wallet swap, no margin account
            involved (the real `/trade/spot` product surface).
        from_asset: Symbol to sell.
        to_asset: Symbol to buy.
        amount: Human-readable amount of `from_asset`.
        authority: Base58 wallet address — the margin owner for
            "can_swap"/"margin_swap", or the wallet itself for "wallet_swap".
        slippage_bps: Slippage tolerance, default 100 (1%).
        provider: "jupiter" or "titan" for can_swap / margin_swap / wallet_swap; omitted = the better quote.
        mode: For "quote" only: "wallet" (default) or "margin" - which account Titan quotes for (Titan builds per
            address; `authority` must be given for Titan to quote at all).

    Returns: a quote dict (read actions) or the schema-v1 write envelope.
    On error: {"error": "contract_error" | "invalid_input" | "account_not_found"
        | "simulation_failed", "message": "..."}
    """
    ctx = get_context()
    swap = ctx.swap

    if action == "quote":
        owner = Pubkey.from_string(authority) if authority else None
        return await swap.quote(from_asset, to_asset, amount, slippage_bps, authority=owner, mode=mode)

    if action == "can_swap":
        if authority is None:
            return {"error": "invalid_input", "message": "'authority' is required for action=can_swap"}
        return await swap.can_swap(Pubkey.from_string(authority), from_asset, to_asset, amount, provider=provider)

    if action == "margin_swap":
        if authority is None:
            return {"error": "invalid_input", "message": "'authority' is required for action=margin_swap"}
        envelope = await swap.margin_swap(Pubkey.from_string(authority), from_asset, to_asset, amount, slippage_bps, provider=provider)
        return envelope.to_dict()

    if action == "wallet_swap":
        if authority is None:
            return {"error": "invalid_input", "message": "'authority' (the wallet) is required for action=wallet_swap"}
        envelope = await swap.wallet_swap(Pubkey.from_string(authority), from_asset, to_asset, amount, slippage_bps, provider=provider)
        return envelope.to_dict()

    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_swap. Supported: quote, can_swap, margin_swap, wallet_swap",
    }
