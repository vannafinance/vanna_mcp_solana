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
    """Basis and Perena vaults on Vanna: list, positions, previews, deposits and withdrawals.

    Vaults: Basis (delta-neutral: holds the asset and shorts it on Phoenix) "basis-sol", "basis-eth", "basis-btc",
    "basis-tsla", "basis-meta" (shares vbSOL, vbETH, vbBTC, vbTSLA, vbMETA); Perena "perena-usdstar" (USD*) and
    "perena-junior" (USD* Junior, 1% instant-exit fee). Every deposit is USDC and every withdrawal pays USDC; the shares
    are held in the margin account as collateral. A deposit or withdrawal moves the margin's USDC: it never borrows.
    SOL Basis also deposits the margin's USDC, in one transaction routed through the wallet (margin USDC -> wallet ->
    vault -> vbSOL back into margin): the program checks the health factor while the USDC is out
    (`health_factor_while_usdc_moves`), and vbSOL minted above the minimum (~0.5%) stays in the wallet for "move_shares".

    Args:
        action: "list" — every vault: NAV, share price, whether deposits are open, deposit limit, hedge (Basis), APY
            (only when the app has enough share-price history; never estimated). "positions" — the wallet's vault
            shares in its margin account (and any SOL Basis shares left in the wallet) with their collateral value.
            "preview" — simulate a deposit (`call`="deposit", `amount` USDC) or withdrawal (`call`="withdraw",
            `amount` shares): expected shares or USDC, the minimum enforced, the health factor after (SOL Basis deposit:
            while the USDC is out of the margin). Nothing is built.
            "deposit" — deposit `amount` USDC. "withdraw" — withdraw `amount` shares to USDC. "move_shares" — move
            SOL Basis shares left in the wallet into the margin account (`amount` shares, or all when omitted).
            A margin write may first return a one-time setup transaction (summary.prerequisite): sign it, then repeat.
        authority: Base58 wallet address (the margin owner); required for every action but "list".
        vault: For "preview" / "deposit" / "withdraw" / "move_shares": a vault id, share symbol or name.
        amount: USDC for a deposit; shares for a withdrawal or move_shares.
        call: For "preview": "deposit" or "withdraw".

    Returns: a dict. On error: {"error": "invalid_input" | "contract_error" | ..., "message": "..."}
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
