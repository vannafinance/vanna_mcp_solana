"""`vanna_plan` — plan strategies and multi-step actions, then run them with the user's approval.

This is the part of the website Copilot that is not language: sizing a leveraged position, ordering an exit so health
only improves, sizing an exact-output swap, checking the wallet can fund each step, freezing the result with exact
amounts, and executing it one step at a time with every step re-checked against live data first. The MCP client
(Claude, Cursor) does the thinking about what the user wants; it calls this tool for every number and every rule.

Flow: draw a plan (`lever`, `unwind`, `swap_exact_out`, `custom`) -> show the user the summary, warnings and worst-case
health factor -> they open `plan_url`, review, and approve with their wallet -> each step is signed in their wallet
in turn -> poll `status` and report only what the chain confirmed. Nothing here signs or sends: the user's wallet does.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp_server import internal
from mcp_server.error_handling import tool_safe
from mcp_server.planning import facts, strategies
from mcp_server.planning.engine import PlanError, get_engine
from mcp_server.planning.funding import check_funding
from mcp_server.planning.preflight import OUT_PREFIX, resolve_percent, validate_steps
from vanna_core import risk
from vanna_core.config import get_settings

PlanAction = Literal["lever", "unwind", "swap_exact_out", "custom", "status", "cancel"]


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": code, "message": message, **extra}


async def _normalize_custom(wallet: str, raw_steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Model-friendly steps -> frozen specs. Amounts are validated, percentages resolved once, never rounded."""
    steps: list[dict[str, Any]] = []
    for i, s in enumerate(raw_steps):
        if not isinstance(s, dict) or not isinstance(s.get("op"), str):
            return None, f"Step {i + 1} needs an 'op'."
        op = s["op"]
        if op == "report":
            steps.append(strategies.report_step())
            continue
        asset = s.get("asset")
        amount = s.get("amount")
        slots: dict[str, Any] = {}
        if op == "swap":
            slots = {"to_asset": s.get("to_asset"), "mode": "margin" if s.get("source") == "margin" else "wallet",
                     "slippage_bps": int(s.get("slippage_bps") or risk.DEFAULT_SLIPPAGE_BPS)}
            if s.get("provider") in ("jupiter", "titan"):
                slots["provider"] = s["provider"]
        if op == "repay":
            slots = {"source": "margin" if s.get("source") == "margin" else "wallet"}
        if isinstance(amount, str) and amount.strip().lower() in ("all", "max", "maximum"):
            amount = "all"
        elif isinstance(amount, str) and amount.strip().endswith("%"):
            try:
                pct = float(amount.strip()[:-1])
            except ValueError:
                return None, f"Step {i + 1}: {amount!r} is not a valid percentage."
            if not 0 < pct <= 100:
                return None, f"Step {i + 1}: percentages must be between 0 and 100."
            if pct >= 100:
                amount = "all"
            else:
                r = await resolve_percent(wallet, op, str(asset), pct, margin=op == "swap" and slots.get("mode") == "margin")
                if not r["ok"]:
                    return None, f"Step {i + 1}: {r['message']}"
                amount = r["amount"]
        elif isinstance(amount, str) and amount.startswith(OUT_PREFIX):
            parsed, issue = risk.parse_amount(amount[len(OUT_PREFIX):], slots.get("to_asset"))
            if issue:
                return None, f"Step {i + 1}: {issue.message}"
            amount = OUT_PREFIX + str(parsed)
        elif amount is not None:
            parsed, issue = risk.parse_amount(amount, asset if asset in facts.PRODUCT_ASSETS else None)
            if issue:
                return None, f"Step {i + 1}: {issue.message}"
            amount = parsed
        steps.append(strategies.write_step(op, asset, amount, **slots))
    return steps, None


@tool_safe
async def vanna_plan(
    action: PlanAction,
    authority: str | None = None,
    asset: str | None = None,
    leverage: float | None = None,
    borrow_asset: str | None = None,
    amount: str | None = None,
    min_hf: float | None = None,
    scope: str = "all",
    close_account: bool = False,
    from_asset: str | None = None,
    to_asset: str | None = None,
    receive_amount: str | None = None,
    source: str = "wallet",
    slippage_bps: int = risk.DEFAULT_SLIPPAGE_BPS,
    steps: list[dict[str, Any]] | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Draw, show and run multi-step plans with exact amounts, a worst-case health factor and the user's approval.

    Use this for anything beyond a single plain action: leverage, closing positions, "swap SOL to get 100 USDC", or a
    chain of steps. The numbers come from this tool, never from you: do not compute amounts yourself.

    Args:
        action:
            "lever" — "lever my SOL 2x": deposit `asset` if it is not already margin collateral, borrow a stable
              (`borrow_asset` USDC or USDT; if omitted the tool returns the choices — ask the user), swap it into
              `asset` inside the margin account, report the final health factor. Sized so health holds the floor even
              at the swap's worst price. `amount` is units of `asset` to lever (omit = the existing margin balance);
              `min_hf` raises the floor (never lowers it); `leverage` is the multiple (above 1, at most 10).
            "unwind" — "close everything": redeem Kamino, repay every debt (swapping other collateral in if short),
              withdraw collateral, redeem Earn — in an order where health only improves. `scope`: all | margin | earn
              | farm; `close_account=true` also closes the emptied margin account (only when asked).
            "swap_exact_out" — "swap SOL to get 100 USDC": the input is sized from live quotes so the minimum received
              covers `receive_amount` of `to_asset`; `source` "margin" swaps collateral inside the margin account,
              "wallet" swaps from the wallet.
            "custom" — a chain you composed (at most 8 steps). `steps` is a list of {op, asset, amount, to_asset?,
              source?, provider?}: provider ("jupiter" | "titan") picks a swap's route (omitted = the better quote); op is one of create_account, earn_supply, earn_redeem, deposit, withdraw, borrow, repay, swap,
              farm_deposit, farm_redeem, close_account, report. `amount` is an exact decimal string, "all", a percent
              like "50%", or for swaps "out:100" (receive at least 100). repay `source` is "wallet" or "margin".
            "status" — the plan's state and each step's (pending, built, submitted, confirmed, skipped, failed,
              uncertain). Poll this after the user starts signing; report only confirmed steps as done.
            "cancel" — stop a plan that has not finished.
        authority: The wallet. Optional when logged in with a wallet: it is the logged-in wallet.
        plan_id: For status/cancel.

    Returns, for a drawn plan: {ok, plan_id, summary, steps, warnings, simulation (hf before/after), hf_floor, digest,
        approve_within_seconds, plan_url, approval_message}. Tell the user the summary and warnings, then ask them to
        open `plan_url`: they review and approve with their wallet there, and sign each step. A plan is valid for
        approval for 5 minutes, then it must be drawn again from fresh data.
    A refusal: {ok: false, message, options?} — explain it plainly; never work around it.
    """
    engine = get_engine()
    settings = get_settings()

    if action in ("status", "cancel"):
        if engine is None:
            return _error("not_available", "Plan execution is not enabled on this server (ENABLE_SIGN_LINKS).")
        if not plan_id:
            return _error("invalid_input", "'plan_id' is required.")
        try:
            return await (engine.status_for(authority, plan_id) if action == "status" else engine.cancel(plan_id, authority))
        except PlanError as exc:
            return _error(exc.code, exc.message)

    if not authority:
        return _error("invalid_input", "'authority' (the wallet) is required.")

    if action == "lever":
        if asset is None or leverage is None:
            return _error("invalid_input", "'asset' and 'leverage' are required for action=lever")
        drawn = await strategies.plan_lever(authority, asset, float(leverage), borrow_asset, amount, min_hf)
    elif action == "unwind":
        drawn = await strategies.plan_unwind(authority, scope, close_account)
    elif action == "swap_exact_out":
        if not (from_asset and to_asset and receive_amount):
            return _error("invalid_input", "'from_asset', 'to_asset' and 'receive_amount' are required for action=swap_exact_out")
        parsed, issue = risk.parse_amount(receive_amount, to_asset if to_asset in facts.PRODUCT_ASSETS else None)
        if issue:
            return _error("invalid_input", issue.message)
        step = strategies.write_step("swap", from_asset, f"{OUT_PREFIX}{parsed}", to_asset=to_asset,
                                     mode="margin" if source == "margin" else "wallet", slippage_bps=int(slippage_bps))
        problem = validate_steps([step])
        if problem:
            return {"ok": False, "message": problem}
        drawn = {"ok": True, "summary": f"{strategies.label_for(step)}.", "steps": [step],
                 "warnings": strategies.plan_warnings([step]), "simulation": None, "hf_floor": settings.min_health_factor}
    elif action == "custom":
        if not steps:
            return _error("invalid_input", "'steps' is required for action=custom")
        norm, problem = await _normalize_custom(authority, steps)
        if problem or norm is None:
            return {"ok": False, "message": problem}
        problem = validate_steps(norm)
        if problem:
            return {"ok": False, "message": problem}
        short = await check_funding(authority, norm)
        if short:
            return {"ok": False, "message": short}
        drawn = {"ok": True, "summary": "Plan: " + ", then ".join(strategies.label_for(s)[0].lower() + strategies.label_for(s)[1:] for s in norm) + ".",
                 "steps": norm, "warnings": strategies.plan_warnings(norm), "simulation": None, "hf_floor": settings.min_health_factor}
    else:
        return _error("invalid_input", f"Unknown action {action!r} for vanna_plan.")

    if not drawn.get("ok"):
        return drawn
    if action in ("lever", "unwind", "swap_exact_out"):
        short = await check_funding(authority, drawn["steps"])
        if short:
            return {"ok": False, "message": short}
    if engine is None:
        return {
            "ok": True, "executable": False, "summary": drawn["summary"], "steps": [
                {"index": i, "label": strategies.label_for(s), "op": s["op"], "asset": s.get("asset"), "amount": s.get("amount"),
                 "slots": s.get("slots")} for i, s in enumerate(drawn["steps"])],
            "warnings": drawn["warnings"], "simulation": drawn.get("simulation"), "hf_floor": drawn["hf_floor"],
            "note": "This server has no signing page (ENABLE_SIGN_LINKS), so the plan can be reviewed but not run. "
                    "Run the steps with the individual write tools, one at a time.",
        }
    try:
        view = await engine.create(authority, drawn, kind=action)
    except PlanError as exc:
        return {"ok": False, "message": exc.message}
    return {"ok": True, "executable": True, **view,
            "next": "Show the user the summary, warnings and worst-case health factor. Ask them to open plan_url, review "
                    "and approve with their wallet, then sign each step. Then poll vanna_plan action=status."}
