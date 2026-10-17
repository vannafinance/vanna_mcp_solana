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
    """Phoenix perpetuals (SOL, ETH, BTC, TSLA, META; long or short), paid from the margin account's USDC.

    A perp is not a Vanna loan: Phoenix's own leverage, position size = collateral x leverage. "Long SOL 3x" with no
    other detail: ask "spot leverage (vanna_plan lever) or a perp?".

    Actions:
      markets — prices, max leverage, fees, open for trading.
      positions — Phoenix balance, withdrawable now, each position (PnL, funding, liquidation prices).
      preview — what an open would do (size, price, fee, health after). Builds nothing. Always before open.
      register — first trade only, when preview says needs_trader_registration.
      open — market, side, collateral_usdc (from margin; "0" = the Phoenix balance), leverage. If it returns
        summary.prerequisite_kind "phoenix_fund", that is a funding transaction: after it lands call open again with
        the same arguments and funded=true.
      close — market; all, or `fraction` (0-1] or `size_usd`.
      withdraw — USDC back to the margin account: all, or `amount_usdc`.
    slippage_bps: default 100 (200 for TSLA, META). authority: the wallet (omit when logged in).
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
