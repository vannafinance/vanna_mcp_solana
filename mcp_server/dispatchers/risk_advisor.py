"""`vanna_risk` — the Copilot's read-side advisor: "how much can I borrow?", "what if I withdraw 1 SOL?", "how far
am I from liquidation?", "what's the most I can lever?", and the product policy itself.

Every figure is computed here from this server's own collateral/debt/price numbers and the program's own pre-flight
checks (`can_borrow` / `can_withdraw` / `can_swap`), at the product's 1.15 safety floor — stricter than the program's
1.10 liquidation line. The model should relay these numbers, never compute its own.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp_server import internal
from mcp_server.error_handling import tool_safe
from mcp_server.internal import ToolError
from mcp_server.planning import facts
from mcp_server.planning.funding import check_funding
from mcp_server.planning.preflight import humanize, validate_steps
from vanna_core import risk
from vanna_core.config import get_settings

RiskAction = Literal["capacity", "what_if", "max_leverage", "liquidation_distance", "funding_check", "policy"]

REFUSALS = [
    "Liquidating anyone (executing a liquidation). Reading whether an account is at risk is fine.",
    "Admin and protocol-configuration actions (register assets, change oracles, collect fees, pause, upgrade...).",
    "Calling arbitrary programs, raw transactions, or sending funds to another address.",
    "Kamino multi-asset liquidity / LP positions (single-asset Kamino lend 'farm' is supported).",
    "Perpetuals, options, futures and short positions (leveraged LONG exposure through margin is supported).",
    "Tokens outside the product (anything but SOL, USDC, USDT, JitoSOL, JupSOL and the Kamino receipts they produce).",
    "Conditional, scheduled or standing orders (limit orders, stop-loss, DCA, 'every week', 'when SOL hits 90'): "
    "this assistant only acts on what is asked right now and cannot watch prices or act later.",
    "Acting for any wallet other than the connected one.",
]


def _policy() -> dict[str, Any]:
    s = get_settings()
    return {
        "health_factor_floor": s.min_health_factor,
        "liquidation_health_factor": risk.LIQUIDATION_HF,
        "thin_buffer": risk.THIN_BUFFER,
        "max_leverage": risk.MAX_LEVERAGE,
        "default_slippage_bps": risk.DEFAULT_SLIPPAGE_BPS,
        "max_action_usd": s.max_action_usd,
        "max_daily_usd": s.max_daily_usd,
        "writes_per_minute": s.write_rate_per_minute,
        "max_plan_steps": s.max_plan_steps,
        "plan_approval_seconds": s.plan_approval_ttl_seconds,
        "assets": {"lend_borrow": list(facts.LEND_BORROW_ASSETS), "collateral_swap_farm_only": list(facts.COLLATERAL_ONLY_ASSETS),
                   "kamino_receipts": ["kSOL", "kUSDC", "kUSDT", "kJitoSOL", "kJupSOL"]},
        "sol_fee_reserve": risk.SOL_FEE_RESERVE,
        "safety_layer_active": bool(s.safety_layer or s.public_mode),
        "refused": REFUSALS,
        "rules": [
            "Health factor = margin collateral USD / debt USD. At or below 1.10 the account can be liquidated; with no debt there is no health factor.",
            "Actions that can lower health (borrow, withdraw, margin swap) are refused below the 1.15 floor and flagged when close to it.",
            "Borrowed funds stay in the margin account and count as collateral.",
            "Amounts are exact decimal strings; an amount with more decimals than the token has is refused, never rounded.",
        ],
    }


@tool_safe
async def vanna_risk(
    action: RiskAction,
    authority: str | None = None,
    what: Literal["borrow", "withdraw"] = "borrow",
    op: Literal["borrow", "withdraw", "repay"] = "borrow",
    asset: str | None = None,
    amount: str | None = None,
    target_hf: float | None = None,
    steps: list[dict[str, Any]] | None = None,
    min_hf: float | None = None,
) -> dict[str, Any]:
    """Risk, capacity and what-if reads at the product's health-factor floor.

    Args:
        action:
            "capacity" — the largest SAFE borrow (`what`="borrow") or collateral withdrawal (`what`="withdraw") that keeps
              health at or above the floor, per asset (all of them when `asset` is omitted), verified through the
              program's own pre-flight. `protocol_max` is the looser program-level maximum (1.10) for reference only.
            "what_if" — "can I borrow 500 USDC?" / "what will my HF be if I withdraw 1 SOL?" / "how much to repay to reach
              HF 2?": `op` borrow|withdraw|repay, `asset`, and an `amount` OR a `target_hf`. Nothing is built.
            "max_leverage" — the highest leverage on `amount` of `asset` (omit = the existing margin balance) that holds the
              floor at the swap's worst price, for a deposit-if-needed lever; plus a table of 1.5x..10x.
            "liquidation_distance" — how far collateral can fall (as a %) before the account reaches 1.10.
            "funding_check" — can the wallet pay for the `steps` of a plan (same step shape as vanna_plan custom).
            "policy" — the product's rules: floor, limits, assets, what is refused. Read it before advising.
        authority: The wallet (optional when logged in with a wallet).
        min_hf: raise the floor for this read (never lower it).

    On error: {"error": "invalid_input" | "account_not_found" | "price_unavailable", "message": "..."}
    """
    settings = get_settings()
    if action == "policy":
        return _policy()
    if not authority:
        return {"error": "invalid_input", "message": "'authority' (the wallet) is required."}
    floor = max(settings.min_health_factor, min_hf or 0.0)

    if action == "funding_check":
        if not steps:
            return {"error": "invalid_input", "message": "'steps' is required for action=funding_check"}
        problem = validate_steps(steps)
        if problem:
            return {"error": "invalid_input", "message": problem}
        short = await check_funding(authority, steps)
        return {"funded": short is None, "message": short}

    h = await facts.load_health(authority)
    if h.kind == "no_account":
        return {"error": "account_not_found", "message": "There is no margin account yet. Open one, then deposit collateral."}
    if h.kind != "ok" or h.health is None:
        return {"error": "contract_error", "message": "The account could not be read, so no figure is given."}
    health = h.health
    c, d = health.collateral_usd, health.debt_usd

    if action == "liquidation_distance":
        try:
            snap = await internal.call("vanna_margin_status", action="liquidation_snapshot", authority=authority)
        except ToolError as exc:
            return {"error": exc.code, "message": exc.message}
        hf = health.hf
        out: dict[str, Any] = {"health_factor": hf, "liquidation_at": risk.LIQUIDATION_HF, "floor": floor,
                               "is_liquidatable": snap.get("health_at_or_below_threshold") is True,
                               "total_collateral_usd": c, "total_debt_usd": d,
                               "note": "Read-only: this assistant never executes a liquidation."}
        out["collateral_drop_to_liquidation_pct"] = None if hf is None else max(0.0, round((1 - risk.LIQUIDATION_HF / hf) * 100, 2))
        out["headline"] = ("No debt, so the account cannot be liquidated." if hf is None else
                           f"If all collateral fell together by about {out['collateral_drop_to_liquidation_pct']:.0f}%, the account would reach the liquidation line.")
        return out

    if action == "capacity":
        if what == "borrow":
            targets = [asset] if asset else list(facts.LEND_BORROW_ASSETS)
            if asset and asset not in facts.LEND_BORROW_ASSETS:
                return {"error": "asset_not_offered", "message": f"{asset} can't be borrowed. Only SOL, USDC and USDT can."}
        else:
            targets = [asset] if asset else [r.symbol for r in health.collateral if r.symbol in facts.PRODUCT_ASSETS]
        if not targets:
            return {"headline": "There is no collateral to withdraw.", "floor": floor, "capacity": []}
        sized = []
        for a in targets:
            s = await (facts.size_safe_borrow(authority, a, hint=h) if what == "borrow" else facts.size_safe_withdraw(authority, a, hint=h))
            row: dict[str, Any] = {"asset": a}
            if s.ok:
                row.update(safe_amount=s.amount, usd=round(s.usd, 2), health_factor_after=s.hf_after, protocol_max=s.protocol_max)
            else:
                row.update(safe_amount="0", reason=s.reason)
            sized.append(row)
        return {"what": what, "floor": floor, "health_factor": health.hf, "capacity": sized,
                "note": f"Sized to the {floor:.2f} safety floor, stricter than the protocol's 1.10 liquidation line, and checked against the program's own rules."}

    if action == "what_if":
        if not asset:
            return {"error": "invalid_input", "message": "'asset' is required for action=what_if"}
        if op == "borrow" and asset not in facts.LEND_BORROW_ASSETS:
            return {"error": "asset_not_offered", "message": f"{asset} can't be borrowed."}
        if amount is None and target_hf is None:
            return {"error": "invalid_input", "message": "Give an 'amount' or a 'target_hf'."}
        if target_hf is not None and not risk.LIQUIDATION_HF < target_hf <= 100:
            return {"error": "invalid_input", "message": "A health factor target has to be above the 1.10 liquidation line."}
        price = await facts.fresh_price(asset)
        if price is None:
            return {"error": "price_unavailable", "message": f"The {asset} price is unavailable right now."}
        dp = risk.token_decimals(asset)
        now_hf = risk.hf_after(c, d)

        def verdict(hf: float | None) -> str:
            if hf is None:
                return "no debt, so no liquidation risk"
            if hf < risk.LIQUIDATION_HF:
                return "below the 1.10 liquidation line"
            if hf < floor:
                return f"above liquidation but under the {floor:.2f} safety floor"
            return f"at or above the {floor:.2f} safety floor"

        if amount is None:  # solve for the amount that reaches the target
            if op == "repay":
                if d == 0:
                    return {"headline": "There is no debt, so there is nothing to repay.", "health_factor": None}
                need = d - c / target_hf
                if need <= 0:
                    return {"headline": f"The health factor is already {now_hf:.2f}, above {target_hf:g}.", "health_factor": now_hf}
                units = risk.floor_decimals(need / price.usd + 10 ** -dp, dp)
                return {"headline": f"To bring the health factor above {target_hf:g}, repay about {units} {asset} (${need:,.2f}).",
                        "repay_amount": units, "repay_usd": round(need, 2), "health_factor_now": now_hf,
                        "note": "Calculated from current collateral and debt value (collateral / debt). It is not a transaction."}
            if op == "borrow":
                max_usd = risk.max_borrow_usd(c, d, target_hf, True)
                if not max_usd > 0:
                    return {"headline": f"No {asset} can be borrowed while staying at a {target_hf:g} health factor or above.", "health_factor_now": now_hf}
                units = risk.floor_decimals(max_usd / price.usd, dp)
                checked = None
                try:
                    can = facts.read_can(await internal.call("vanna_margin_status", action="can_borrow", authority=authority, asset=asset, amount=units))
                    checked = can.projected_hf if can and isinstance(can.projected_hf, float) else None
                except ToolError:
                    pass
                return {"headline": f"About {units} {asset} (${max_usd:,.2f}) can be borrowed and keep the health factor at {target_hf:g} or above.",
                        "borrow_amount": units, "borrow_usd": round(max_usd, 2), "program_projected_hf": checked,
                        "note": "Calculated from current collateral and debt value."}
            return {"error": "invalid_input", "message": "Give an amount to withdraw and the health factor after it is checked."}

        parsed, issue = risk.parse_amount(amount, asset)
        if issue:
            return {"error": "invalid_input", "message": issue.message}
        usd = float(parsed) * price.usd
        if op == "repay":
            after = risk.hf_after(c, max(d - usd, 0.0))
            return {"headline": f"Repaying {parsed} {asset} (${usd:,.2f}) would leave the health factor at about {'no debt' if after is None else f'{after:.2f}'}.",
                    "health_factor_now": now_hf, "health_factor_after": after, "debt_after_usd": round(max(d - usd, 0.0), 2),
                    "verdict": verdict(after), "note": "Calculated from current collateral and debt value; not a transaction."}
        try:
            can = facts.read_can(await internal.call(
                "vanna_margin_status", action="can_borrow" if op == "borrow" else "can_withdraw",
                authority=authority, asset=asset, amount=parsed))
        except ToolError as exc:
            return {"error": exc.code, "message": humanize(exc)}
        if can is None:
            return {"error": "contract_error", "message": "That check returned an unreadable result."}
        hf = can.projected_hf if isinstance(can.projected_hf, float) else None
        ok = can.allowed and (hf is None or hf >= floor)
        head = (f"Yes. {'Borrowing' if op == 'borrow' else 'Withdrawing'} {parsed} {asset} (${usd:,.2f}) would leave the health factor at "
                f"{'no debt' if hf is None else f'{hf:.2f}'}, {verdict(hf)}." if ok else
                f"{'Borrowing' if op == 'borrow' else 'Withdrawing'} {parsed} {asset} is allowed by the protocol but not advisable: the health factor would be {hf:.2f}, {verdict(hf)}."
                if can.allowed else f"No. {can.reason or 'The protocol would not allow that.'}")
        return {"headline": head, "allowed": can.allowed, "within_floor": ok, "health_factor_now": now_hf,
                "health_factor_after": hf, "liquidation_at": risk.LIQUIDATION_HF,
                "note": "Checked with the protocol's own rules; this is a question, nothing was prepared."}

    if action == "max_leverage":
        a = asset or "SOL"
        p = await facts.fresh_price(a)
        if p is None:
            return {"error": "price_unavailable", "message": f"The {a} price is unavailable right now."}
        margin_units = health.collateral_row(a).units if health.collateral_row(a) else 0.0
        if amount is not None:
            parsed, issue = risk.parse_amount(amount, a)
            if issue:
                return {"error": "invalid_input", "message": issue.message}
            equity_units = float(parsed)
            deposit_units = max(0.0, equity_units - margin_units)
        else:
            equity_units, deposit_units = margin_units, 0.0
        if not equity_units > 0:
            return {"error": "invalid_input", "message": f"There is no {a} to lever. Pass an 'amount' to deposit from the wallet."}
        top = risk.max_leverage(collateral_usd=c, debt_usd=d, equity_usd=equity_units * p.usd,
                                deposit_usd=deposit_units * p.usd, floor=floor, slippage_bps=risk.DEFAULT_SLIPPAGE_BPS)
        table = []
        for lev in (1.5, 2, 3, 4, 5, 6, 8, 10):
            s = risk.size_lever(collateral_usd=c, debt_usd=d, equity_usd=equity_units * p.usd, deposit_usd=deposit_units * p.usd,
                                leverage=lev, floor=floor)
            table.append({"leverage": lev, "allowed": s.ok, "worst_case_hf": s.hf_after if s.ok else None})
        return {"asset": a, "equity_units": risk.floor_decimals(equity_units, risk.token_decimals(a)), "floor": floor,
                "max_leverage": round(math_floor_1(top), 1), "table": table,
                "note": "Sized with the swap at its worst allowed price (1% slippage) so the floor holds even on a bad fill."}

    return {"error": "invalid_input", "message": f"Unknown action {action!r} for vanna_risk."}


def math_floor_1(x: float) -> float:
    import math

    return math.floor(x * 10) / 10 if x != float("inf") else x
