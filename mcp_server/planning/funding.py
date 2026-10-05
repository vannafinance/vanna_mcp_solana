"""Funding check at preview time: can the wallet pay for the steps that spend wallet funds?

Port of the Copilot's `workflow/funding.ts`. Only steps that run before anything could change the balance are
checked (a borrow, withdrawal, redeem or farm exit may bring funds in, so what comes after them is checked when its
turn comes). Figures come from the wallet summary; the one constant is the SOL this server keeps back for fees and rent.
"""

from __future__ import annotations

from typing import Any

from mcp_server import internal
from vanna_core import risk

# Ops that pull from the wallet: deposit, Earn supply, and repay/swap when their source is the wallet.
CREATES_FUNDS = frozenset({"borrow", "withdraw", "earn_redeem", "farm_redeem"})
# Ops whose transaction can create accounts (token accounts, the margin account) and so pay rent.
PAYS_RENT = frozenset({"create_account", "deposit", "earn_supply", "swap", "farm_deposit"})


def _spends_wallet(step: dict[str, Any]) -> bool:
    if step.get("kind", "write") != "write":
        return False
    op, slots = step["op"], step.get("slots") or {}
    if op in ("deposit", "earn_supply"):
        return True
    if op == "repay":
        return str(slots.get("source") or "wallet") != "margin"
    if op == "swap":
        return str(slots.get("mode") or "wallet") != "margin"
    return False


async def check_funding(wallet: str, steps: list[dict[str, Any]]) -> str | None:
    writes = [s for s in steps if s.get("kind", "write") == "write"]
    if not writes:
        return None
    try:
        w = await internal.call("vanna_wallet", action="summary", authority=wallet)
    except Exception:  # noqa: BLE001 — an unreadable wallet is reported by the step's own check
        return None
    sol = risk.num(w.get("sol_balance"))
    if sol is None:
        return None

    def held(symbol: str) -> float | None:
        if symbol == "SOL":
            return sol
        rows = w.get("assets") if isinstance(w.get("assets"), list) else []
        row = next((a for a in rows if isinstance(a, dict) and a.get("symbol") == symbol), None)
        return risk.num(row.get("wallet_balance")) if row else 0.0

    if any(s["op"] in PAYS_RENT for s in writes) and sol < risk.SOL_FEE_RESERVE:
        return (f"The wallet has {sol:g} SOL. Transactions here need about {risk.SOL_FEE_RESERVE} SOL for network fees and "
                "account rent, so this can't be prepared yet. Add SOL to the wallet first.")
    planned: dict[str, float] = {}
    for s in steps:
        if s.get("kind", "write") == "write" and s["op"] in CREATES_FUNDS:
            break
        if not _spends_wallet(s) or not s.get("asset"):
            continue
        asset = s["asset"]
        have = held(asset)
        if have is None:
            continue
        spendable = max(0.0, have - risk.SOL_FEE_RESERVE) if asset == "SOL" else have
        already = planned.get(asset, 0.0)
        amount = s.get("amount")
        if amount == "all":
            need = already + 1e-12 if spendable > already else already + 1
        elif isinstance(amount, str) and amount.startswith(("out:", "%")):
            continue
        else:
            try:
                need = already + float(amount)
            except (TypeError, ValueError):
                continue
        if spendable + 1e-9 < need:
            have_text = (f"{have:g} SOL (about {risk.SOL_FEE_RESERVE} SOL stays back for fees and rent)" if asset == "SOL"
                         else f"{have:g} {asset}")
            need_text = f"some {asset}" if amount == "all" else f"{need:g} {asset}"
            return f"The wallet has {have_text}, but this needs {need_text}. Add funds or ask for a smaller amount."
        planned[asset] = need
    return None
