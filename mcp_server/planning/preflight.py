"""Per-step pre-flight: fresh checks against live data, then the exact tool call that builds the step.

A port of the Copilot's `preflightStep` (lib/copilot/workflow/steps.ts). Nothing here moves funds: a step either
resolves to the one tool call that builds an unsigned, simulated transaction for the user's wallet, is skipped
(already true), or is refused with a reason. "All" and percentages resolve against live balances here, so accrued
interest and exact Kamino redemptions are always current.

Every tool name and argument is this server's own (`authority` for margin/swap/farm/wallet, `owner` for earn).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp_server import internal
from mcp_server.internal import ToolError
from mcp_server.planning import facts
from mcp_server.planning.facts import HealthRead
from mcp_server.safety import get_safety
from vanna_core import risk
from vanna_core.config import get_settings

OUT_PREFIX = "out:"
EXACT_OUT_ROUNDS = 4

# Ops that lower (or can lower) the health factor: gated by the risk floor.
RISK_INCREASING = frozenset({"borrow", "withdraw", "swap"})
# Ops that touch the margin account (need one, and a health read).
MARGIN_OPS = frozenset({"deposit", "withdraw", "borrow", "repay", "farm_deposit", "farm_redeem", "close_account"})
WRITE_OPS = (
    "create_account", "close_account", "earn_supply", "earn_redeem", "deposit", "withdraw", "borrow", "repay",
    "swap", "farm_deposit", "farm_redeem",
)
# Which assets an op accepts: lend/borrow assets only, any product asset, or none.
OP_ASSETS = {
    "create_account": "none", "close_account": "none", "earn_supply": "lend", "earn_redeem": "lend",
    "deposit": "any", "withdraw": "any", "borrow": "lend", "repay": "lend", "swap": "any",
    "farm_deposit": "any", "farm_redeem": "any",
}


def out_target(amount: str | None) -> str | None:
    return amount[len(OUT_PREFIX):] if amount and amount.startswith(OUT_PREFIX) else None


@dataclass
class Prepared:
    usd: float
    tool: str
    args: dict[str, Any]
    risk: risk.RiskResult
    simulation: dict[str, Any] | None
    resolved_amount: str | None
    notes: list[str] = field(default_factory=list)


@dataclass
class Preflight:
    """ok + prepared: build it. ok + skip: nothing to do. not ok: refused (`code`, `message`)."""

    ok: bool
    prepared: Prepared | None = None
    skip: str | None = None
    code: str = ""
    message: str = ""


def _fail(code: str, message: str) -> Preflight:
    return Preflight(False, code=code, message=message)


def humanize(exc_or_text: Any) -> str:
    """A short human sentence for a tool error or a failed simulation (the raw reason can be a screen of logs)."""
    text = (exc_or_text.message if isinstance(exc_or_text, ToolError) else str(exc_or_text or "")).strip()
    if not text:
        return "The action could not be prepared."
    patterns = (
        (("insufficient funds", "insufficientfunds"),"The wallet does not hold enough of that asset (or enough SOL for fees) for this action."),
        (("stalepri", "0x178b", "stale_price", "price_unavailable"), "A price feed is stale right now, so the program refuses to use it. Try again in a minute."),
        (("slippagetoleranceexceeded", "0x1771", "6001"), "The swap price moved past the allowed slippage. Try again for a fresh quote."),
        (("would breach", "healthfactor", "0x1790", "unhealthy"), "The action would push the account below the protocol's health limit."),
        (("account_not_found", "accountnotinitialized", "3012"), "There is no margin account yet."),
        (("transaction_too_large", "size limit", "exceeds solana", "too large for one"), "That swap route is too large for one Solana transaction. Try a different pair, or swap in the wallet instead of inside the margin account."),
        (("insufficient pool liquidity",), "The pool does not have enough available liquidity for that."),
        (("borrow_cap", "supply_cap", "cap exceeded"), "That would exceed the pool's cap."),
    )
    low = text.lower()
    for needles, sentence in patterns:
        if any(n in low for n in needles):
            return sentence
    first = next((ln for ln in text.splitlines() if ln.strip()), text)
    if any(k in first for k in ("TransactionError", "InstructionError", "Custom(", "custom program error")):
        return "The network rejected this step when it was simulated. Try again in a moment, or try a smaller amount."
    return first if len(first) <= 220 else first[:217] + "..."


def _simulation(health: HealthRead | None, hf_after_value: Any, collateral_after: float, debt_after: float,
                amount_usd: float, asset: str | None, margin_applicable: bool) -> dict[str, Any]:
    projected = None if hf_after_value is risk.UNKNOWN else hf_after_value
    return {
        "hf_before": health.hf if health else None,
        "hf_after": projected,
        "collateral_before": health.collateral_usd if health else 0.0,
        "collateral_after": max(0.0, collateral_after),
        "debt_before": health.debt_usd if health else 0.0,
        "debt_after": max(0.0, debt_after),
        "liquidation_threshold": risk.LIQUIDATION_HF,
        "amount_usd": amount_usd,
        "asset": asset,
        "margin_applicable": margin_applicable,
    }


async def size_exact_out(wallet: str, from_asset: str, to_asset: str, target: float, slippage_bps: int) -> dict[str, Any]:
    """The input of `from_asset` whose quoted MINIMUM output (after slippage) is at least `target` of `to_asset`.

    Starts from oracle prices plus the slippage allowance, then corrects from real quotes. Swaps are built by input
    amount; this keeps "receive at least N" honest without a separate exact-output route."""
    if not target > 0:
        return {"ok": False, "message": "The amount to receive has to be above zero."}
    p_from, p_to = await facts.fresh_price(from_asset), await facts.fresh_price(to_asset)
    if p_from is None or p_to is None:
        return {"ok": False, "message": f"{from_asset} and {to_asset} could not be priced right now, so this swap can't be sized."}
    amount_in = (target * p_to.usd / p_from.usd) * (1 + slippage_bps / 10_000) * 1.005
    for _ in range(EXACT_OUT_ROUNDS):
        text = risk.ceil_units(amount_in, from_asset)
        try:
            quote = await internal.call("vanna_swap", action="quote", from_asset=from_asset, to_asset=to_asset,
                                        amount=text, slippage_bps=slippage_bps)
        except ToolError as exc:
            return {"ok": False, "message": humanize(exc)}
        min_out = risk.num(quote.get("min_out_amount"))
        if min_out is None or not min_out > 0:
            return {"ok": False, "message": "The swap route returned no usable quote."}
        if min_out + 1e-12 >= target:
            return {"ok": True, "amount_in": text, "min_out": str(min_out), "out_amount": quote.get("out_amount")}
        amount_in = float(text) * (target / min_out) * 1.002
    return {"ok": False, "message": f"No quote delivers at least {target} {to_asset} right now. Try again in a moment."}


async def resolve_percent(wallet: str, op: str, asset: str, pct: float, *, margin: bool = False) -> dict[str, Any]:
    """"50% of my X" resolved once against the live figure it is a share of; the frozen plan then carries an exact amount."""
    base: float | None = None
    try:
        if op in ("deposit", "earn_supply") or (op == "swap" and not margin):
            base = await facts.wallet_spendable(wallet, asset)
        elif op == "earn_redeem":
            base = risk.num((await internal.call("vanna_earn_position", action="deposited_balance", owner=wallet, asset=asset)).get("redeemable_underlying"))
        elif op == "farm_redeem":
            farm = await internal.call("vanna_farm_overview", action="positions", authority=wallet)
            rows = farm.get("positions") if isinstance(farm.get("positions"), list) else []
            row = next((p for p in rows if isinstance(p.get("symbol"), str) and p["symbol"].lower() == f"k{asset}".lower()), None)
            base = risk.num(row.get("receipt_balance")) if row else None
        elif op == "borrow":
            sized = await facts.size_safe_borrow(wallet, asset)
            if not sized.ok:
                return {"ok": False, "message": sized.reason}
            base = float(sized.amount)
        else:
            h = await facts.load_health(wallet)
            if h.kind == "no_account":
                return {"ok": False, "message": "There is no margin account yet."}
            if h.kind != "ok" or h.health is None:
                return {"ok": False, "message": "The account could not be read to work out that percentage."}
            row = (h.health.debt_row if op == "repay" else h.health.collateral_row)(asset)
            base = row.units if row else 0.0
    except ToolError as exc:
        return {"ok": False, "message": humanize(exc)}
    if base is None:
        return {"ok": False, "message": "The balance that percentage applies to could not be read."}
    if not base > 0:
        return {"ok": False, "message": f"There is no {asset} for that."}
    amount = risk.floor_decimals(base * pct / 100, risk.token_decimals(asset))
    if not float(amount) > 0:
        return {"ok": False, "message": "That percentage works out to an amount too small to use."}
    return {"ok": True, "amount": amount}


async def preflight_step(wallet: str, step: dict[str, Any]) -> Preflight:
    """Fresh checks for one write step against live data. `step`: {op, asset, amount, slots}."""
    settings = get_settings()
    floor = settings.min_health_factor
    op, asset, amount = step["op"], step.get("asset"), step.get("amount")
    slots = step.get("slots") or {}
    notes: list[str] = []
    if step.get("kind", "write") != "write":
        return _fail("invalid", "Only write steps are built.")

    if op == "create_account":
        try:
            if (await internal.call("vanna_account", action="resolve", authority=wallet)).get("exists") is True:
                return Preflight(True, skip="A margin account already exists.")
        except ToolError as exc:
            return _fail("unavailable", humanize(exc))
        return Preflight(True, prepared=Prepared(0.0, "vanna_account", {"action": "create", "authority": wallet},
                                                 risk.RiskResult("allow"), None, None, notes))
    if op == "close_account":
        h = await facts.load_health(wallet)
        if h.kind == "no_account":
            return Preflight(True, skip="There is no margin account.")
        if h.kind != "ok" or h.health is None:
            return _fail("unavailable", "The account could not be read, so it will not be closed.")
        if any(r.units > 0 for r in h.health.collateral) or h.health.debt:
            return _fail("invalid", "A margin account can only be closed when it holds no collateral and no debt. Repay and withdraw everything first.")
        return Preflight(True, prepared=Prepared(0.0, "vanna_account", {"action": "close", "authority": wallet},
                                                 risk.RiskResult("allow"), None, None, notes))

    if not asset:
        return _fail("invalid", "This step is missing its asset.")
    price = await facts.fresh_price(asset)
    if price is None:
        return _fail("unavailable", f"The {asset} price is unavailable, so this action can't be valued against the limits.")
    # A price still flagged stale after the refresh does not block the build: the build refreshes, simulates and
    # fails closed (StalePrice) if a price is genuinely too old. The number is current; only its flag lags.

    is_margin_swap = op == "swap" and slots.get("mode") == "margin"
    margin = op in MARGIN_OPS or is_margin_swap
    hr = await facts.load_health(wallet) if margin else None
    if hr is not None and hr.kind == "no_account":
        return _fail("no_account", "There is no margin account yet. Open one first.")
    if hr is not None and hr.kind != "ok":
        return _fail("unavailable", "The margin account could not be read just now, so this is not being built.")
    health = hr.health if hr is not None else None

    usd = 0.0
    hf_after_value: Any = health.hf if health else risk.UNKNOWN
    collateral_after = health.collateral_usd if health else 0.0
    debt_after = health.debt_usd if health else 0.0
    tool, args = "", {}

    if op == "earn_supply":
        if amount == "all":
            spend = await facts.wallet_spendable(wallet, asset)
            if spend is None or not spend > 0:
                return _fail("invalid", f"The wallet has no spendable {asset}.")
            amount = risk.floor_decimals(spend, risk.token_decimals(asset))
        usd = float(amount) * price.usd
        tool, args = "vanna_earn_write", {"action": "supply", "owner": wallet, "asset": asset, "amount": amount}

    elif op == "earn_redeem":
        try:
            pos = await internal.call("vanna_earn_position", action="deposited_balance", owner=wallet, asset=asset)
        except ToolError as exc:
            return _fail("unavailable", humanize(exc))
        shares, redeemable = risk.num(pos.get("share_balance")), risk.num(pos.get("redeemable_underlying"))
        if shares is None or redeemable is None:
            return _fail("unavailable", "The Earn balance could not be read.")
        if not shares > 0:
            return _fail("invalid", f"Nothing is supplied to the {asset} Earn pool.")
        if slots.get("shares"):  # a direct `redeem` names the shares to burn
            burn = str(slots["shares"])
            if float(burn) > shares + 1e-12:
                return _fail("invalid", f"Only {pos['share_balance']} pool shares are held, fewer than {burn}.")
            amount = risk.floor_decimals(float(burn) / shares * redeemable, risk.token_decimals(asset))
        elif amount == "all" or float(amount) >= redeemable:
            burn, amount = str(pos["share_balance"]), str(pos["redeemable_underlying"])
        else:
            burn = risk.floor_decimals(float(amount) * shares / redeemable, risk.token_decimals(asset))
        if not float(burn) > 0:
            return _fail("invalid", "That amount is too small to redeem.")
        usd = float(amount) * price.usd
        tool, args = "vanna_earn_write", {"action": "redeem", "owner": wallet, "asset": asset, "shares": burn}
        notes.append(f"Burns {burn} pool shares for about {amount} {asset}.")

    elif op == "deposit":
        if amount == "all":
            spend = await facts.wallet_spendable(wallet, asset)
            if spend is None or not spend > 0:
                return _fail("invalid", f"The wallet has no spendable {asset}.")
            amount = risk.floor_decimals(spend, risk.token_decimals(asset))
        usd = float(amount) * price.usd
        collateral_after = (health.collateral_usd if health else 0.0) + usd
        hf_after_value = risk.hf_after(collateral_after, debt_after)
        tool, args = "vanna_margin_trade", {"action": "deposit", "authority": wallet, "asset": asset, "amount": amount}

    elif op == "withdraw":
        if amount == "all":
            sized = await facts.size_safe_withdraw(wallet, asset, hint=hr)
            if not sized.ok:
                return _fail("risk", sized.reason)
            amount = sized.amount
        usd = float(amount) * price.usd
        try:
            can = facts.read_can(await internal.call("vanna_margin_status", action="can_withdraw", authority=wallet, asset=asset, amount=amount))
        except ToolError as exc:
            return _fail("unavailable", humanize(exc))
        if can is None:
            return _fail("unavailable", "This withdrawal could not be checked against the health factor.")
        if not can.allowed:
            return _fail("risk", humanize(can.reason))
        hf_after_value = can.projected_hf
        collateral_after = (health.collateral_usd if health else 0.0) - usd
        tool, args = "vanna_margin_trade", {"action": "withdraw", "authority": wallet, "asset": asset, "amount": amount}

    elif op == "borrow":
        if amount == "all":
            sized = await facts.size_safe_borrow(wallet, asset, hint=hr)
            if not sized.ok:
                return _fail("risk", sized.reason)
            amount = sized.amount
        usd = float(amount) * price.usd
        try:
            can = facts.read_can(await internal.call("vanna_margin_status", action="can_borrow", authority=wallet, asset=asset, amount=amount))
        except ToolError as exc:
            return _fail("unavailable", humanize(exc))
        if can is None:
            return _fail("unavailable", "This borrow could not be checked against the health factor.")
        if not can.allowed:
            return _fail("risk", humanize(can.reason))
        hf_after_value = can.projected_hf
        collateral_after = (health.collateral_usd if health else 0.0) + usd  # borrowed funds stay in the margin vault
        debt_after = (health.debt_usd if health else 0.0) + usd
        tool, args = "vanna_margin_trade", {"action": "borrow", "authority": wallet, "asset": asset, "amount": amount}

    elif op == "repay":
        row = health.debt_row(asset) if health else None
        if row is None or not row.units > 0:
            return _fail("invalid", f"There is no {asset} debt to repay.")
        repay_all = amount == "all" or float(amount) >= row.units
        usd = row.usd if repay_all else float(amount) * price.usd
        from_margin = slots.get("source") == "margin"
        debt_after = max(0.0, (health.debt_usd if health else 0.0) - usd)
        if from_margin:
            collateral_after = max(0.0, (health.collateral_usd if health else 0.0) - usd)
        hf_after_value = risk.hf_after(collateral_after, debt_after)
        if repay_all:
            amount = row.units_text
            notes.append("Repays the full outstanding balance, including interest accrued until it lands.")
        base: dict[str, Any] = {"authority": wallet, "asset": asset, **({"repay_all": True} if repay_all else {"amount": amount})}
        tool, args = ("vanna_margin_trade", {"action": "repay", **base}) if from_margin else (
            "vanna_margin_trade", {"action": "repay_from_wallet", "payer": wallet, **base})

    elif op == "swap":
        to_asset = str(slots.get("to_asset") or "")
        if not to_asset:
            return _fail("invalid", "This swap is missing the asset to buy.")
        slippage = int(risk.num(slots.get("slippage_bps")) or risk.DEFAULT_SLIPPAGE_BPS)
        target = out_target(amount)
        if target:
            sized = await size_exact_out(wallet, asset, to_asset, float(target), slippage)
            if not sized["ok"]:
                return _fail("unavailable", sized["message"])
            available = (health.collateral_row(asset).units if health and health.collateral_row(asset) else 0.0) if is_margin_swap \
                else (await facts.wallet_spendable(wallet, asset) or 0.0)
            where = "the margin account holds" if is_margin_swap else "the wallet can spend"
            if available + 1e-9 < float(sized["amount_in"]):
                return _fail("invalid", f"To receive at least {target} {to_asset} about {sized['amount_in']} {asset} is needed, and {where} {risk.floor_decimals(available, risk.token_decimals(asset))} {asset}.")
            amount = sized["amount_in"]
            notes.append(f"Sized from a live quote so at least {target} {to_asset} is received: spending {amount} {asset}, minimum received {sized['min_out']} {to_asset}.")
        if amount == "all":
            if is_margin_swap:
                row = health.collateral_row(asset) if health else None
                if row is None or not row.units > 0:
                    return _fail("invalid", f"There is no {asset} collateral to swap.")
                amount = row.units_text
            else:
                spend = await facts.wallet_spendable(wallet, asset)
                if spend is None or not spend > 0:
                    return _fail("invalid", f"The wallet has no spendable {asset}.")
                amount = risk.floor_decimals(spend, risk.token_decimals(asset))
        usd = float(amount) * price.usd
        swap_args = {"from_asset": asset, "to_asset": to_asset, "amount": amount, "slippage_bps": slippage}
        try:
            quote = await internal.call("vanna_swap", action="quote", **swap_args)
            if risk.num(quote.get("out_amount")) is not None:
                notes.append(f"Quote: about {quote['out_amount']} {to_asset} (minimum {quote.get('min_out_amount')}).")
        except ToolError:
            pass  # the build below is the authoritative route check
        if is_margin_swap:
            try:
                can = facts.read_can(await internal.call("vanna_swap", action="can_swap", authority=wallet, **swap_args))
            except ToolError as exc:
                return _fail("unavailable", humanize(exc))
            if can is None:
                return _fail("unavailable", "This swap could not be checked against the health factor.")
            if not can.allowed:
                return _fail("risk", humanize(can.reason))
            hf_after_value = can.projected_hf
            tool, args = "vanna_swap", {"action": "margin_swap", "authority": wallet, **swap_args}
        else:
            tool, args = "vanna_swap", {"action": "wallet_swap", "authority": wallet, **swap_args}

    elif op == "farm_deposit":
        row = health.collateral_row(asset) if health else None
        if amount == "all":
            if row is None or not row.units > 0:
                return _fail("invalid", f"There is no {asset} collateral in the margin account to farm.")
            amount = row.units_text
        elif row is None or row.units + 1e-9 < float(amount):
            return _fail("invalid", f"The margin account holds {row.units_text if row else '0'} {asset}, less than {amount}. Deposit the difference first.")
        usd = float(amount) * price.usd
        tool, args = "vanna_farm_kamino", {"action": "deposit", "authority": wallet, "underlying": asset, "amount": amount}
        notes.append("Converts margin collateral into the Kamino receipt token, which still counts as collateral.")

    elif op == "farm_redeem":
        try:
            farm = await internal.call("vanna_farm_overview", action="positions", authority=wallet)
        except ToolError as exc:
            return _fail("no_account" if exc.code == "account_not_found" else "unavailable", humanize(exc))
        rows = farm.get("positions") if isinstance(farm.get("positions"), list) else []
        pos = next((p for p in rows if isinstance(p.get("symbol"), str) and p["symbol"].lower() == f"k{asset}".lower()), None)
        receipt = risk.num(pos.get("receipt_balance")) if pos else None
        underlying = risk.num(pos.get("underlying_amount")) if pos else None
        if pos is None or receipt is None or underlying is None or not receipt > 0:
            return _fail("invalid", f"There is no Kamino {asset} position to redeem.")
        if amount == "all" or float(amount) >= receipt:
            amount, out = str(pos["receipt_balance"]), underlying
        else:
            out = float(amount) / receipt * underlying
        usd = out * price.usd
        # The amount is in k{asset} receipt tokens, not the underlying: say what it is worth.
        notes.append(f"That is {amount} k{asset} receipt tokens, worth about {round(out, 6)} {asset}.")
        tool, args = "vanna_farm_kamino", {"action": "redeem", "authority": wallet, "underlying": asset, "amount": amount}
    else:
        return _fail("invalid", "That step is not supported.")

    if not (usd == usd and usd >= 0):
        return _fail("unavailable", "This action could not be valued, so it is not allowed.")
    spend_check = await get_safety().check_spend(wallet, usd)
    if not spend_check.ok:
        return Preflight(False, code="limit", message=spend_check.message)
    gate = risk.risk_gate(
        increases_risk=op in RISK_INCREASING and not (op == "swap" and not is_margin_swap),
        hf_after_value=hf_after_value, floor=floor,
    )
    if gate.decision == "block":
        return _fail("risk", " ".join(gate.reasons))
    sim = _simulation(health, hf_after_value, collateral_after, debt_after, usd, asset, health is not None)
    return Preflight(True, prepared=Prepared(usd, tool, args, gate, sim, amount, notes))


def validate_steps(steps: list[dict[str, Any]]) -> str | None:
    """A plan must be non-empty, bounded, and only contain steps the executor knows how to build."""
    cap = get_settings().max_plan_steps
    if not steps:
        return "That plan has no steps."
    if len(steps) > cap:
        return f"A plan can have at most {cap} steps."
    for s in steps:
        if s.get("kind", "write") == "read":
            if s.get("op") != "report":
                return "Unsupported read step."
            continue
        op = s.get("op")
        if op not in WRITE_OPS:
            return f"Unsupported step {op!r}."
        rule = OP_ASSETS[op]
        asset = s.get("asset")
        if rule != "none":
            if not asset:
                return "A step is missing its asset."
            if asset not in facts.PRODUCT_ASSETS:
                return f"{asset} is not part of the product. The assets are SOL, USDC, USDT (lend and borrow) and JitoSOL, JupSOL (collateral, swap and Kamino farm)."
            if rule == "lend" and asset not in facts.LEND_BORROW_ASSETS:
                return f"{asset} can be collateral, swapped or farmed on Kamino, but has no Vanna lending pool, so it can't be lent or borrowed."
            if not s.get("amount"):
                return "A step is missing its amount."
        if op == "swap":
            to = (s.get("slots") or {}).get("to_asset")
            if not to or to not in facts.PRODUCT_ASSETS:
                return "A swap needs a product asset to buy."
            if to == asset:
                return "Pick two different assets to swap."
    return None
