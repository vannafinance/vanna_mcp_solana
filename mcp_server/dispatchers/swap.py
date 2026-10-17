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
    """Swap one token for another with an EXACT amount to sell, through Jupiter or Titan (like the app's Spot page).

    "Swap to receive 100 USDC", "swap half / all my SOL" -> vanna_plan (swap_exact_out, or custom with "50%" / "all").

    Actions:
      quote — both providers' quotes (`providers`, best first) and the better one on top. If the user named no
        provider, show both and ask which; pass it as `provider` (omitted = the better quote at build time).
        `mode` "wallet" (default) or "margin" says which account Titan quotes for (needs `authority`).
      can_swap — would a swap inside the margin account keep the health factor safe.
      margin_swap — swap collateral inside the margin account (from_asset must already be collateral).
      wallet_swap — a plain swap in the wallet; no margin account involved.
    from_asset, to_asset, amount (of from_asset); slippage_bps default 100. authority: the wallet (omit when logged in).
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
