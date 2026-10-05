"""`vanna_margin_trade` — collateral/borrow/repay writes (TOOLS_CATALOGUE.md
§3.5). Every action returns the schema-v1 write envelope (vanna_core/envelope.py).
This dispatcher never signs or broadcasts anything.
"""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

MarginTradeAction = Literal[
    "deposit", "withdraw", "close_collateral", "borrow", "close_debt",
    "repay", "repay_from_wallet", "deposit_and_borrow", "close_position",
]


@tool_safe
async def vanna_margin_trade(
    action: MarginTradeAction,
    authority: str,
    asset: str | None = None,
    amount: str | None = None,
    repay_all: bool = False,
    payer: str | None = None,
    deposit_asset: str | None = None,
    deposit_amount: str | None = None,
    borrow_asset: str | None = None,
    borrow_amount: str | None = None,
) -> dict[str, Any]:
    """Build an unsigned margin-account collateral/borrow/repay transaction.

    Args:
        action: "deposit" — deposit `amount` of `asset` as collateral. "withdraw"
            — withdraw `amount` of `asset` (fails closed if it would breach the
            1.10 health threshold). "close_collateral" — reclaim the rent of an
            emptied collateral vault. "borrow" — borrow `amount` of `asset` into the
            margin vault (the first borrow of an asset opens its debt position itself).
            "close_debt" — reclaim the rent of a fully repaid debt position. "repay" —
            repay from the margin account's own vault (`amount`, or all debt
            if `repay_all`). "repay_from_wallet" — repay from the OWNER's wallet: the
            tokens are deposited into the margin vault and repaid in the same
            transaction (a repay-all deposits the debt plus a 0.2% accrual buffer,
            which stays as collateral). Only the owner can repay now, so `payer`
            must be the owner.
        authority: Base58 wallet address that owns the margin account.
        asset: Required for every action except "repay_from_wallet" doesn't
            need `authority` to sign (see `payer`).
        amount: Human-readable amount; required for deposit/withdraw/borrow,
            optional for repay/repay_from_wallet when `repay_all=True`.
        repay_all: For "repay"/"repay_from_wallet" — repay the full
            outstanding debt instead of a capped `amount`.
        payer: For "repay_from_wallet" only — the wallet paying; must equal `authority`.
        deposit_asset, deposit_amount, borrow_asset, borrow_amount: For
            "deposit_and_borrow" only — deposit `deposit_amount` of
            `deposit_asset`, then borrow `borrow_amount` of `borrow_asset`
            (same or different asset), in one transaction.

    "close_position" takes no extra args — it re-reads real on-chain state
    on every call and returns the single next applicable transaction (repay
    all debt, then withdraw each collateral position one at a time, then
    reclaim the rent of any empty vault / closed debt position, then close
    the margin account) — call it repeatedly until `summary.next_step == "done"`.

    Returns: the schema-v1 write envelope.
    On error: {"error": "contract_error" | "invalid_input" | "account_not_found"
        | "simulation_failed", "message": "..."}
    """
    ctx = get_context()
    trade = ctx.margin_trade

    if action == "deposit":
        if asset is None or amount is None:
            return {"error": "invalid_input", "message": "'asset' and 'amount' are required for action=deposit"}
        envelope = await trade.deposit_collateral(Pubkey.from_string(authority), asset, amount)
        return envelope.to_dict()

    if action == "withdraw":
        if asset is None or amount is None:
            return {"error": "invalid_input", "message": "'asset' and 'amount' are required for action=withdraw"}
        envelope = await trade.withdraw_collateral(Pubkey.from_string(authority), asset, amount)
        return envelope.to_dict()

    if action == "close_collateral":
        if asset is None:
            return {"error": "invalid_input", "message": "'asset' is required for action=close_collateral"}
        envelope = await trade.close_collateral_position(Pubkey.from_string(authority), asset)
        return envelope.to_dict()

    if action == "borrow":
        if asset is None or amount is None:
            return {"error": "invalid_input", "message": "'asset' and 'amount' are required for action=borrow"}
        envelope = await trade.borrow(Pubkey.from_string(authority), asset, amount)
        return envelope.to_dict()

    if action == "close_debt":
        if asset is None:
            return {"error": "invalid_input", "message": "'asset' is required for action=close_debt"}
        envelope = await trade.close_debt_position(Pubkey.from_string(authority), asset)
        return envelope.to_dict()

    if action == "repay":
        if asset is None or (amount is None and not repay_all):
            return {
                "error": "invalid_input",
                "message": "'asset' is required, and either 'amount' or repay_all=True, for action=repay",
            }
        envelope = await trade.repay_from_margin(Pubkey.from_string(authority), asset, amount or "0", repay_all)
        return envelope.to_dict()

    if action == "repay_from_wallet":
        if asset is None or payer is None or (amount is None and not repay_all):
            return {
                "error": "invalid_input",
                "message": (
                    "'asset', 'payer', and either 'amount' or repay_all=True "
                    "are required for action=repay_from_wallet"
                ),
            }
        envelope = await trade.repay_from_wallet(
            Pubkey.from_string(payer), Pubkey.from_string(authority), asset, amount or "0", repay_all
        )
        return envelope.to_dict()

    if action == "deposit_and_borrow":
        if deposit_asset is None or deposit_amount is None or borrow_asset is None or borrow_amount is None:
            return {
                "error": "invalid_input",
                "message": (
                    "'deposit_asset', 'deposit_amount', 'borrow_asset', and 'borrow_amount' "
                    "are all required for action=deposit_and_borrow"
                ),
            }
        envelope = await trade.deposit_and_borrow(
            Pubkey.from_string(authority), deposit_asset, deposit_amount, borrow_asset, borrow_amount
        )
        return envelope.to_dict()

    if action == "close_position":
        envelope = await trade.close_position(Pubkey.from_string(authority))
        return envelope.to_dict()

    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_margin_trade.",
    }
