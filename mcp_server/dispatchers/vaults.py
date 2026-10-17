"""`vanna_vaults` — Basis (delta-neutral) and Perena vaults, the app's Farm page (vanna-credit-backend `2462473`).

Reads (the vaults with NAV and share price, the wallet's vault shares, a simulated deposit / withdrawal preview) and
writes (deposit USDC, withdraw shares to USDC, move SOL Basis shares left in the wallet into margin), each returned
unsigned for the user's wallet. Vault shares are margin collateral and count toward the health factor.
"""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

VaultsAction = Literal["list", "positions", "preview", "deposit", "withdraw", "move_shares"]


@tool_safe
async def vanna_vaults(
    action: VaultsAction,
    authority: str | None = None,
    vault: str | None = None,
    amount: str | None = None,
    call: Literal["deposit", "withdraw"] | None = None,
) -> dict[str, Any]:
    """Basis and Perena vaults: margin USDC in, vault shares (kept as margin collateral) out. Never borrows.

    Vaults: basis-sol, basis-eth, basis-btc, basis-tsla, basis-meta (delta-neutral: hold the asset, short it on
    Phoenix; shares vbSOL ...), perena-usdstar (USD*), perena-junior (USD* Junior, 1% instant-exit fee).

    Actions:
      list — NAV, share price, deposits open, limits, APY (empty until the app has history: never estimate it).
      positions — the wallet's vault shares and their value.
      preview — call="deposit" (amount USDC) or "withdraw" (amount shares): expected result, health after. Always
        before a deposit or withdraw.
      deposit — amount USDC. withdraw — amount shares, paid in USDC.
      move_shares — SOL Basis shares left in the wallet into the margin account (amount, or all).
    A write may first return a one-time setup transaction (summary.prerequisite): have it signed, then call again.
    authority: the wallet (omit when logged in).
    """
    ctx = get_context()
    vaults = ctx.vaults

    if action == "list":
        return await vaults.list_vaults()

    if authority is None:
        return {"error": "invalid_input", "message": f"'authority' is required for action={action}"}
    owner = Pubkey.from_string(authority)

    if action == "positions":
        return await vaults.positions(owner)

    if vault is None:
        return {"error": "invalid_input", "message": f"'vault' is required for action={action}"}

    if action == "move_shares":
        return (await vaults.move_shares(owner, vault, amount)).to_dict()

    if amount is None:
        return {"error": "invalid_input", "message": f"'amount' is required for action={action}"}

    if action == "preview":
        if call is None:
            return {"error": "invalid_input", "message": "'call' (deposit or withdraw) is required for action=preview"}
        return await vaults.preview(owner, vault, call, amount)

    if action == "deposit":
        return (await vaults.deposit(owner, vault, amount)).to_dict()

    if action == "withdraw":
        return (await vaults.withdraw(owner, vault, amount)).to_dict()

    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_vaults. Supported: list, positions, preview, deposit, withdraw, move_shares",
    }
