"""Direct write calls under the safety layer.

When the model calls a write tool directly ("supply 100 USDC"), the Copilot's rule is that a single action is a
one-step plan: it takes the same pre-flight, approval, exactly-once and confirmation path as a long one. This module
maps a tool call to the plan step it is, so `vanna_earn_write supply` and a plan's "earn_supply" step are checked by
the same code (`preflight_step`) — the 1.15 floor, the spend caps and the funding checks cannot differ between them.

Calls with no plan equivalent (deposit_and_borrow, close_position, the account lifecycle plumbing) never raise risk
beyond what the program's own 1.10 check enforces, or are repeatable reductions, so they get the limits check only.
"""

from __future__ import annotations

from typing import Any

from mcp_server.planning import facts
from mcp_server.planning.preflight import Prepared, Preflight, humanize, preflight_step
from mcp_server.safety import get_safety
from vanna_core import risk
from vanna_core.config import get_settings


def step_from_call(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """The plan step a direct write call is, or None when it has no single-step equivalent."""
    action = args.get("action")
    asset, amount = args.get("asset"), args.get("amount")
    if tool == "vanna_account" and action == "create":
        return {"kind": "write", "op": "create_account", "asset": None, "amount": None, "slots": {}}
    if tool == "vanna_account" and action == "close":
        return {"kind": "write", "op": "close_account", "asset": None, "amount": None, "slots": {}}
    if tool == "vanna_earn_write" and action == "supply" and amount:
        return {"kind": "write", "op": "earn_supply", "asset": asset, "amount": amount, "slots": {}}
    if tool == "vanna_earn_write" and action == "redeem":
        return {"kind": "write", "op": "earn_redeem", "asset": asset, "amount": "all",
                "slots": {"shares": args.get("shares")}}
    if tool == "vanna_margin_trade":
        if action in ("deposit", "withdraw", "borrow") and asset and amount:
            return {"kind": "write", "op": action, "asset": asset, "amount": amount, "slots": {}}
        if action == "repay" and asset:
            return {"kind": "write", "op": "repay", "asset": asset,
                    "amount": "all" if args.get("repay_all") else amount, "slots": {"source": "margin"}}
        if action == "repay_from_wallet" and asset and args.get("authority") == args.get("payer"):
            return {"kind": "write", "op": "repay", "asset": asset,
                    "amount": "all" if args.get("repay_all") else amount, "slots": {"source": "wallet"}}
    if tool == "vanna_swap" and action in ("margin_swap", "wallet_swap"):
        return {"kind": "write", "op": "swap", "asset": args.get("from_asset"), "amount": args.get("amount"),
                "slots": {"to_asset": args.get("to_asset"), "slippage_bps": args.get("slippage_bps", risk.DEFAULT_SLIPPAGE_BPS),
                          "mode": "margin" if action == "margin_swap" else "wallet",
                          **({"provider": args["provider"]} if args.get("provider") else {})}}
    if tool == "vanna_farm_kamino" and action in ("deposit", "redeem"):
        return {"kind": "write", "op": f"farm_{action}", "asset": args.get("underlying"), "amount": args.get("amount"), "slots": {}}
    return None


def describe_call(tool: str, args: dict[str, Any]) -> str:
    """One plain sentence for what a direct write does (the plan page headline)."""
    from mcp_server.planning.strategies import label_for

    step = step_from_call(tool, args)
    if step is not None:
        return label_for(step)
    action = args.get("action")
    if tool == "vanna_margin_trade" and action == "deposit_and_borrow":
        return (f"Deposit {args.get('deposit_amount')} {args.get('deposit_asset')} and borrow "
                f"{args.get('borrow_amount')} {args.get('borrow_asset')} in one transaction")
    if tool == "vanna_margin_trade" and action == "close_position":
        return "Close your margin position (the next step of the unwind)"
    if tool == "vanna_vaults":
        from vanna_core.contracts.vaults import VaultContract

        try:
            name = VaultContract.find(str(args.get("vault") or "")).name
        except ValueError:
            name = str(args.get("vault") or "the vault")
        if action == "deposit":
            return f"Deposit {args.get('amount')} USDC into {name}"
        if action == "withdraw":
            return f"Withdraw {args.get('amount')} {name} shares to USDC"
        if action == "move_shares":
            return f"Move your {name} shares from the wallet into your margin account"
    if tool == "vanna_perps":
        market = str(args.get("market") or "").upper().removesuffix("-PERP")
        if action == "register":
            return "Create your Phoenix trading account (first perps trade only)"
        if action == "open":
            usdc = str(args.get("collateral_usdc") or "0")
            funds = "your Phoenix balance" if usdc in ("0", "0.0") else f"{usdc} USDC from your margin account"
            return f"Open a {args.get('leverage')}x {args.get('side')} on {market}-PERP with {funds} (Phoenix)"
        if action == "close":
            part = "all of" if args.get("fraction") in (None, 1, 1.0) and not args.get("size_usd") else "part of"
            return f"Close {part} your {market}-PERP position (Phoenix)"
        if action == "withdraw":
            return f"Move {args.get('amount_usdc') or 'all your'} USDC from Phoenix back to your margin account"
    return f"{tool.replace('vanna_', '').replace('_', ' ')}: {action}"


def check_amounts(tool: str, args: dict[str, Any]) -> str | None:
    """A typed amount that cannot be used as written is refused here, never rounded or reinterpreted."""
    pairs: list[tuple[Any, str | None]] = [
        (args.get("amount"), args.get("asset") or args.get("underlying") or args.get("from_asset")),
        (args.get("deposit_amount"), args.get("deposit_asset")),
        (args.get("borrow_amount"), args.get("borrow_asset")),
        (args.get("shares"), args.get("asset")),
    ]
    for value, symbol in pairs:
        if value is None or value == "all":
            continue
        _, issue = risk.parse_amount(value, symbol if symbol in facts.PRODUCT_ASSETS else None)
        if issue:
            return issue.message
    return None


async def guard_direct_write(wallet: str, tool: str, args: dict[str, Any], *, consume_rate: bool = True) -> Preflight:
    """Pre-flight for a direct write: the same checks a plan step gets. The caller then builds with `args` as given."""
    issue = check_amounts(tool, args)
    if issue:
        return Preflight(False, code="invalid", message=issue)
    if consume_rate:  # a plan step already spends its build budget in the engine
        rate = await get_safety().consume_write(wallet)
        if not rate.ok:
            return Preflight(False, code="limit", message=rate.message)
    step = step_from_call(tool, args)
    if step is None and tool == "vanna_margin_trade" and args.get("action") == "deposit_and_borrow":
        return await _guard_deposit_and_borrow(wallet, args)
    if step is None:
        # A vault deposit spends its USDC amount (USDC is valued at $1 here: the deposit token is always USDC).
        usd = float(args["amount"]) if tool == "vanna_vaults" and args.get("action") == "deposit" and args.get("amount") else 0.0
        spend = await get_safety().check_spend(wallet, usd)
        if not spend.ok:
            return Preflight(False, code="limit", message=spend.message)
        return Preflight(True, prepared=Prepared(usd, tool, args, risk.RiskResult("allow"), None, None, []))
    result = await preflight_step(wallet, step)
    if result.ok and result.prepared is not None:
        # Build exactly what the caller asked for: pre-flight resolved "all"/percent figures only to check them.
        result.prepared.tool, result.prepared.args = tool, args
    return result


async def _guard_deposit_and_borrow(wallet: str, args: dict[str, Any]) -> Preflight:
    """The one risk-raising composite with no single-step equivalent: deposit then borrow in one transaction. Borrowed funds
    stay in the vault and count as collateral, so HF after = (C + deposit + borrow) / (D + borrow); held at the floor."""
    dep_asset, bor_asset = args.get("deposit_asset"), args.get("borrow_asset")
    if bor_asset not in facts.LEND_BORROW_ASSETS:
        return Preflight(False, code="invalid", message=f"{bor_asset} can't be borrowed. Only SOL, USDC and USDT can.")
    if dep_asset not in facts.PRODUCT_ASSETS:
        return Preflight(False, code="invalid", message=f"{dep_asset} is not part of the product.")
    p_dep, p_bor = await facts.fresh_price(dep_asset), await facts.fresh_price(bor_asset)
    if p_dep is None or p_bor is None:
        return Preflight(False, code="unavailable", message="A price is unavailable, so this can't be valued against the limits.")
    h = await facts.load_health(wallet)
    if h.kind == "unavailable" or h.kind == "malformed":
        return Preflight(False, code="unavailable", message="The margin account could not be read just now, so this is not being built.")
    health = h.health if h.kind == "ok" else None
    dep_usd = float(args["deposit_amount"]) * p_dep.usd
    bor_usd = float(args["borrow_amount"]) * p_bor.usd
    c, d = (health.collateral_usd if health else 0.0), (health.debt_usd if health else 0.0)
    hf_after_value = risk.hf_after(c + dep_usd + bor_usd, d + bor_usd)
    spend = await get_safety().check_spend(wallet, max(dep_usd, bor_usd))
    if not spend.ok:
        return Preflight(False, code="limit", message=spend.message)
    gate = risk.risk_gate(increases_risk=True, hf_after_value=hf_after_value, floor=get_settings().min_health_factor)
    if gate.decision == "block":
        return Preflight(False, code="risk", message=" ".join(gate.reasons))
    return Preflight(True, prepared=Prepared(max(dep_usd, bor_usd), "vanna_margin_trade", args, gate, None, None, []))


def error_for(result: Preflight) -> dict[str, Any]:
    """A refused pre-flight as the tool-error shape every dispatcher uses."""
    code = {"limit": "limit_exceeded", "risk": "health_check_failed", "unavailable": "price_unavailable",
            "no_account": "account_not_found", "invalid": "invalid_input"}.get(result.code, "invalid_input")
    return {"error": code, "message": result.message or humanize(None), "safety_layer": True}


def floor() -> float:
    return get_settings().min_health_factor
