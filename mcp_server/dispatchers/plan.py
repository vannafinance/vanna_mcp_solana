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

from mcp_server.error_handling import tool_safe
from mcp_server.planning import amounts, facts, strategies
from mcp_server.planning.engine import PlanError, get_engine
from mcp_server.planning.funding import check_funding
from mcp_server.planning.preflight import OUT_PREFIX, validate_steps
from vanna_core import risk
from vanna_core.config import get_settings

PlanAction = Literal["lever", "leverage_borrow", "unwind", "swap_exact_out", "custom", "status", "cancel"]


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": code, "message": message, **extra}


async def _normalize_custom(wallet: str, raw_steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]] | None, str | None, list[str]]:
    """Model-friendly steps -> frozen specs. Every amount form (`planning/amounts.py`) is resolved here by the server:
    exact, all, percent, usd, leverage, health target, previous, out. Returns (steps, problem, notes)."""
    steps: list[dict[str, Any]] = []
    notes: list[str] = []
    book = amounts.Book(wallet)
    for i, s in enumerate(raw_steps):
        if not isinstance(s, dict) or not isinstance(s.get("op"), str):
            return None, f"Step {i + 1} needs an 'op'.", notes
        op = s["op"]
        if op == "report":
            steps.append(strategies.report_step())
            continue
        asset = s.get("asset")
        slots: dict[str, Any] = {}
        if op == "swap":
            slots = {"to_asset": s.get("to_asset"), "mode": "margin" if s.get("source") == "margin" else "wallet",
                     "slippage_bps": int(s.get("slippage_bps") or risk.DEFAULT_SLIPPAGE_BPS)}
            if s.get("provider") in ("jupiter", "titan"):
                slots["provider"] = s["provider"]
        if op == "repay":
            slots = {"source": "margin" if s.get("source") == "margin" else "wallet"}
        r = await amounts.resolve(book, op, asset, s.get("amount"), slots, steps, s)
        if not r.ok:
            return None, f"Step {i + 1}: {r.message}", notes
        notes.extend(r.notes)
        step = strategies.write_step(op, asset, r.amount, **slots)
        steps.append(step)
        await book.apply(step)
    return steps, None, notes


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
    """Plan and run anything with more than one step, or any amount the server has to size. The main write tool.

    You never compute an amount: pass the user's words in the amount vocabulary below and the server sizes it.

    Pick the action from what the user says:
      DEPOSIT + BORROW at a leverage: "deposit 10 SOL and borrow USDC at 3x" / "take a 2x loan of USDC against
        depositing 1 JitoSOL" / "borrow USDC at 2x against my SOL"
          -> action="leverage_borrow": asset, amount (to deposit now; omit = borrow against what is already in margin),
             leverage, borrow_asset. It deposits, then borrows (N-1) x the deposit's value, which STAYS in the margin
             account. NO swap.
      LEVER an asset on its own: "lever my SOL 2x" / "go 3x long SOL" / "lever 5 JitoSOL 3x"
          -> action="lever": asset, leverage, borrow_asset (a stable), amount (units of asset to lever; omit = what is
             in margin; deposited first if not there). It borrows a stable and SWAPS it into the asset (a leveraged long),
             sized so the floor holds at the swap's worst price.
      Ask for borrow_asset only if the user named none.
      "close everything" / "exit my positions" -> action="unwind" (scope all | margin | earn | farm).
      "swap SOL to get 100 USDC" -> action="swap_exact_out".
      any other chain (up to 8 steps) -> action="custom".
      "is it done?" -> action="status"; "cancel it" -> action="cancel" (no signature needed).

    Amount vocabulary (custom steps; leverage_borrow's `amount`):
      "10" exact | "all" / "max" (a borrow or withdraw: the most the health floor and the pool allow) | "50%" |
      "usd:500" | "leverage:3" (borrow only; base = this plan's deposits, else `base_asset`+`base_amount` on the step,
      else the account's equity) | "health:2" (borrow / withdraw / repay sized to land on that health factor) |
      "previous" (the step before's amount) | "out:100" (swap: receive at least 100).

    Args:
        action: leverage_borrow | lever | unwind | swap_exact_out | custom | status | cancel.
        authority: The wallet. Omit when logged in with a wallet.
        asset, amount, leverage, borrow_asset, min_hf: for leverage_borrow and lever. min_hf raises the 1.15 floor.
        scope, close_account: for unwind (close_account only when the user asks to close the account).
        from_asset, to_asset, receive_amount, source ("wallet" | "margin"), slippage_bps: for swap_exact_out.
        steps: for custom, [{op, asset, amount, to_asset?, source?, provider?, base_asset?, base_amount?}]. op: deposit,
            withdraw, borrow, repay (source wallet | margin), swap (to_asset; source margin = inside the margin
            account), earn_supply, earn_redeem, farm_deposit, farm_redeem, create_account, close_account, report.
        plan_id: for status / cancel.

    Returns {ok, summary, steps, warnings, simulation (health before / after), plan_url, ...}. Tell the user the summary,
    the health factor and the warnings, then send them to plan_url to approve and sign with their own wallet. Report a
    step as done only when status says confirmed. A refusal is {ok: false, message, options?}: say it plainly and offer
    the options it gives; do not work around it.
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
    elif action == "leverage_borrow":
        if leverage is None:
            return _error("invalid_input", "'leverage' (the multiple, e.g. 2) is required for action=leverage_borrow")
        drawn = await strategies.plan_leverage_borrow(authority, asset, float(leverage), borrow_asset, amount, min_hf)
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
        norm, problem, notes = await _normalize_custom(authority, steps)
        if problem or norm is None:
            return {"ok": False, "message": problem}
        problem = validate_steps(norm)
        if problem:
            return {"ok": False, "message": problem}
        short = await check_funding(authority, norm)
        if short:
            return {"ok": False, "message": short}
        drawn = {"ok": True, "summary": "Plan: " + ", then ".join(strategies.label_for(s)[0].lower() + strategies.label_for(s)[1:] for s in norm) + ".",
                 "steps": norm, "warnings": strategies.plan_warnings(norm, notes), "simulation": await strategies.simulate(authority, norm),
                 "hf_floor": settings.min_health_factor}
    else:
        return _error("invalid_input", f"Unknown action {action!r} for vanna_plan.")

    if not drawn.get("ok"):
        return drawn
    if action in ("lever", "leverage_borrow", "unwind", "swap_exact_out"):
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
