"""`vanna_perps` — Phoenix perpetuals (SOL, ETH, BTC, TSLA, META), the app's Perps page.

Reads (market data, the margin account's Phoenix positions, an order preview with the app's own sizing and checks)
and trades (open / close a position, move USDC back), each returned unsigned for the user's wallet. Perps leverage is Phoenix's own: a perps position never borrows from Vanna; the margin
account's USDC moves into Phoenix as collateral and the Phoenix balance counts toward the margin's health factor.
"""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

PerpsAction = Literal["markets", "positions", "preview", "register", "open", "close", "withdraw"]


@tool_safe
async def vanna_perps(
    action: PerpsAction,
    authority: str | None = None,
    market: str | None = None,
    side: Literal["long", "short"] | None = None,
    collateral_usdc: str | None = None,
    leverage: float | None = None,
    slippage_bps: int | None = None,
    fraction: float | None = None,
    size_usd: str | None = None,
    amount_usdc: str | None = None,
    funded: bool = False,
) -> dict[str, Any]:
    """Phoenix perps on Vanna: markets, positions, order previews, and opening / closing positions.

    Perps are NOT margin borrowing: a perps position is opened on Phoenix with the margin account's USDC as collateral
    and Phoenix's own leverage (position size = collateral x leverage); no Vanna loan is created. Margin borrowing is
    `vanna_margin_trade`; spot leverage is `vanna_plan lever`.

    Args:
        action: "markets" — every listed market (SOL, ETH, BTC, TSLA, META): index and mark price, max leverage,
            whether it is open for new positions, taker fee, default slippage. "positions" — the margin account's
            Phoenix balance, what can be withdrawn now, and each position (side, size, entry, PnL, funding, equity,
            the price at which the Phoenix balance reaches zero, and the liquidation price of the whole margin
            account). "preview" — what opening or adding to a position would do, with the app's checks: size, limit
            price, fee, projected health factor and liquidation price. Nothing is built or sent.
            "register" — the first Phoenix trade's extra step: creates the margin's Phoenix trading account (needed when
            preview says needs_trader_registration). "open" — open or add to a long / short: moves `collateral_usdc`
            of margin USDC into Phoenix and places an immediate-or-cancel market order of collateral x leverage, in one
            transaction; on an account holding many assets that is too much for one transaction, and open returns only
            the funding transaction (summary.prerequisite_kind "phoenix_fund"): once it lands, call open again with the
            same arguments and funded=true. "close" — close the whole position in `market`, or part of it (`fraction` 0-1, or
            `size_usd`), with a reduce-only order. "withdraw" — move USDC from Phoenix back to the margin account (all,
            or `amount_usdc`), only what Phoenix pays now. Writes return an unsigned transaction for the wallet.
        authority: Base58 wallet address (the margin owner); required for every action but "markets".
        market: For "preview" / "open" / "close": "SOL", "ETH", "BTC", "TSLA" or "META" ("-PERP" optional).
        side: For "preview" / "open": "long" or "short".
        collateral_usdc: For "preview" / "open": USDC moved from the margin account into Phoenix for this order; "0"
            uses the Phoenix balance already there.
        leverage: For "preview" / "open": 1 up to the market's max leverage.
        funded: For "preview" / "open": true when an earlier "phoenix_fund" transaction of this same open already moved
            `collateral_usdc` into Phoenix; the order is sized from it and nothing more moves.
        slippage_bps: For "preview" / "open" / "close": worst-price protection; default 100 (1%), 200 for TSLA and META.
        fraction: For "close": the share of the position to close (0-1]; omitted = all of it.
        size_usd: For "close": close this USD size instead of a fraction.
        amount_usdc: For "withdraw": USDC to move back; omitted = all of it.

    Returns: a dict. On error: {"error": "invalid_input" | "contract_error" | ..., "message": "..."}
    """
    ctx = get_context()
    perps = ctx.perps

    if action == "markets":
        return await perps.markets()

    if authority is None:
        return {"error": "invalid_input", "message": f"'authority' is required for action={action}"}
    owner = Pubkey.from_string(authority)

    if action == "positions":
        return await perps.positions(owner)

    if action == "preview":
        missing = [n for n, v in (("market", market), ("side", side), ("collateral_usdc", collateral_usdc), ("leverage", leverage)) if v is None]
        if missing:
            return {"error": "invalid_input", "message": f"{', '.join(repr(m) for m in missing)} required for action=preview"}
        return await perps.preview(owner, market, side, collateral_usdc, float(leverage), slippage_bps, funded=funded)

    if action == "register":
        return (await perps.register(owner)).to_dict()

    if action == "open":
        missing = [n for n, v in (("market", market), ("side", side), ("collateral_usdc", collateral_usdc), ("leverage", leverage)) if v is None]
        if missing:
            return {"error": "invalid_input", "message": f"{', '.join(repr(m) for m in missing)} required for action=open"}
        return (await perps.open(owner, market, side, collateral_usdc, float(leverage), slippage_bps, funded=funded)).to_dict()

    if action == "close":
        if market is None:
            return {"error": "invalid_input", "message": "'market' is required for action=close"}
        return (await perps.close(owner, market, fraction=fraction, size_usd=size_usd, slippage_bps=slippage_bps)).to_dict()

    if action == "withdraw":
        return (await perps.withdraw(owner, amount_usdc)).to_dict()

    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_perps. Supported: markets, positions, preview, register, open, close, withdraw",
    }
