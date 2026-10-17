"""`vanna_margin_trade` — collateral/borrow/repay writes (TOOLS_CATALOGUE.md
§3.5). Every action returns the schema-v1 write envelope (vanna_core/envelope.py).
This dispatcher never signs or broadcasts anything.
"""

from __future__ import annotations

from vanna_core.assets import assert_depositable

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
    """One margin action with an EXACT amount the user typed: deposit, withdraw, borrow, repay.

    Any amount the server must size — "max", "50%", "$500", "3x leverage", "to health 2", "deposit and borrow at 2x" —
    goes to vanna_plan instead (lever or custom). Never compute an amount for this tool.

    Actions:
      deposit / withdraw / borrow — `asset`, `amount`. Borrowed funds stay in the margin account as collateral.
      repay — from the margin balance: `amount`, or repay_all=true.
      repay_from_wallet — from the owner's wallet (payer = the owner): `amount` or repay_all (adds a 0.2% buffer
        that stays as collateral).
      deposit_and_borrow — deposit_asset + deposit_amount, borrow_asset + borrow_amount, in one transaction.
      close_collateral / close_debt — reclaim the rent of an emptied vault / repaid debt position.
      close_position — the single next step to empty and close the account; call again until
        summary.next_step == "done".
    authority: the wallet (omit when logged in). Returns an unsigned transaction and a link for the user to sign.
    """
    ctx = get_context()
    trade = ctx.margin_trade

    if action == "deposit":
        if asset is None or amount is None:
            return {"error": "invalid_input", "message": "'asset' and 'amount' are required for action=deposit"}
        assert_depositable(asset)
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
        assert_depositable(deposit_asset)
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
