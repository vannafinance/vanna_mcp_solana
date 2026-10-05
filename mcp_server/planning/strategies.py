"""Named strategies, planned from live reads: "lever X Nx" and "close everything" (unwind).

Ports of the Copilot's `workflow/strategy.ts` and `workflow/unwind.ts`. A strategy returns a frozen list of steps with
exact amounts (only "all" stays live and says so), the worst-case health factor, and warnings — or a refusal that
says why and what would work instead. Sizing is arithmetic on this server's own collateral/debt/price numbers
(`vanna_core.risk`), with the swap taken at its worst allowed price so the floor holds even on a bad fill.
"""

from __future__ import annotations

from typing import Any

from mcp_server import internal
from mcp_server.internal import ToolError
from mcp_server.planning import facts
from mcp_server.planning.preflight import OUT_PREFIX, humanize
from vanna_core import risk
from vanna_core.config import get_settings

_RECEIPTS = facts.RECEIPT_TO_UNDERLYING


def write_step(op: str, asset: str | None = None, amount: str | None = None, **slots: Any) -> dict[str, Any]:
    return {"kind": "write", "op": op, "asset": asset, "amount": amount, "slots": slots}


def report_step() -> dict[str, Any]:
    return {"kind": "read", "op": "report", "asset": None, "amount": None, "slots": {"report": "health"}}


def label_for(step: dict[str, Any]) -> str:
    op, asset, amount, slots = step["op"], step.get("asset"), step.get("amount"), step.get("slots") or {}
    q = f"{'all' if amount == 'all' else amount or ''} {asset or ''}".strip()
    if op == "create_account":
        return "Open your margin account"
    if op == "close_account":
        return "Close your (empty) margin account"
    if op == "earn_supply":
        return f"Supply {q} to the Vanna Earn pool"
    if op == "earn_redeem":
        return f"Redeem {q} from the Vanna Earn pool"
    if op == "deposit":
        return f"Deposit {q} as margin collateral"
    if op == "withdraw":
        return f"Withdraw {q} from margin collateral"
    if op == "borrow":
        return f"Borrow {q} against your margin collateral"
    if op == "repay":
        return f"Repay {q} {'from your margin balance' if slots.get('source') == 'margin' else 'from your wallet'}"
    if op == "swap":
        to = str(slots.get("to_asset") or "")
        where = "inside your margin account" if slots.get("mode") == "margin" else "in your wallet"
        if isinstance(amount, str) and amount.startswith(OUT_PREFIX):
            return f"Swap {asset} to receive at least {amount[len(OUT_PREFIX):]} {to} {where}"
        return f"Swap {q} to {to} {where}"
    if op == "farm_deposit":
        return f"Supply {q} to Kamino (farm) from your margin balance"
    if op == "farm_redeem":
        what = f"all your k{asset}" if not amount or amount == "all" else f"{amount} k{asset}"
        return f"Redeem {what} from Kamino (farm) back to margin"
    if op == "report":
        return "Report your health factor"
    return op


def plan_warnings(steps: list[dict[str, Any]], extra: list[str] | None = None) -> list[str]:
    warnings = list(extra or [])
    n = sum(1 for s in steps if s.get("kind", "write") == "write")
    if n > 1:
        warnings.append(
            f"{n} separate wallet signatures. The plan stops if you cancel partway, and every step is re-checked "
            "against live data before it is built."
        )
    if any(s.get("kind", "write") == "write" and s.get("amount") == "all" for s in steps):
        warnings.append('"All" is resolved against your live balance at the moment that step is built.')
    return warnings


def _no(message: str, options: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "message": message}
    if options:
        out["options"] = options
    return out


# ------------------------------------------------------------------ lever


async def plan_lever(wallet: str, asset: str, leverage: float, borrow_asset: str | None, amount: str | None,
                     min_hf: float | None = None) -> dict[str, Any]:
    """Lever = (deposit X if it is not already margin collateral) -> borrow a stable -> swap it into X INSIDE the
    margin account (`margin_swap` leaves X in the vault as collateral) -> report the final health factor."""
    settings = get_settings()
    if asset not in facts.PRODUCT_ASSETS:
        return _no(f"{asset} is not part of the product.")
    if not (leverage == leverage) or leverage <= 1:
        return _no("Leverage has to be above 1x.")
    if leverage > risk.MAX_LEVERAGE:
        return _no("I won't size above 10x; at this protocol's health rules it could never hold the 1.15 floor.")
    if borrow_asset is None:
        stables = [b for b in ("USDC", "USDT") if b != asset]
        return _no(
            f"To lever {asset} {leverage:g}x a stable is borrowed and swapped into {asset}. Which one should be borrowed?",
            [{"label": f"Borrow {b}", "borrow_asset": b} for b in stables],
        )
    if borrow_asset not in ("USDC", "USDT"):
        return _no("Leverage is funded by borrowing USDC or USDT.")
    if borrow_asset == asset:
        return _no("Borrowing the same asset you are levering adds no exposure.")
    if amount is not None:
        parsed, issue = risk.parse_amount(amount, asset)
        if issue:
            return _no(issue.message)
        amount = parsed

    floor = max(settings.min_health_factor, min_hf or 0.0)
    h = await facts.load_health(wallet)
    p_asset, p_borrow = await facts.fresh_price(asset), await facts.fresh_price(borrow_asset)
    if h.kind == "unavailable":
        return _no(humanize(h.error))
    if h.kind == "malformed":
        return _no("The account data could not be read, so a leveraged position can't be sized.")
    if p_asset is None:
        return _no(f"The {asset} price is unavailable right now, so this can't be sized safely. Try again in a minute.")
    if p_borrow is None:
        return _no(f"The {borrow_asset} price is unavailable right now, so this can't be sized safely. Try again in a minute.")

    has_account = h.kind == "ok" and h.health is not None
    health = h.health if has_account else None
    margin_units = (health.collateral_row(asset).units if health and health.collateral_row(asset) else 0.0)
    deposit_units = 0.0
    if amount:
        equity_units = float(amount)
        deposit_units = max(0.0, equity_units - margin_units)
    else:
        if not margin_units > 0:
            return _no(
                f"There is no {asset} collateral in the margin account. Say how much {asset} to lever, for example "
                f"'lever 5 {asset} {leverage:g}x', and it is deposited from the wallet first."
            )
        equity_units = margin_units
    if not equity_units > 0:
        return _no("The amount to lever has to be above zero.")
    if deposit_units > 0:
        spendable = await facts.wallet_spendable(wallet, asset)
        if spendable is None:
            return _no("The wallet balance could not be read, so the deposit can't be planned.")
        if spendable + 1e-9 < deposit_units:
            return _no(f"The wallet has {risk.floor_decimals(spendable, 9)} {asset} available"
                       f"{' (keeping a little SOL for fees)' if asset == 'SOL' else ''}, less than the "
                       f"{risk.floor_decimals(deposit_units, 9)} {asset} this plan would deposit.")

    equity_usd, deposit_usd = equity_units * p_asset.usd, deposit_units * p_asset.usd
    collateral_usd = health.collateral_usd if health else 0.0
    debt_usd = health.debt_usd if health else 0.0
    size = risk.size_lever(collateral_usd=collateral_usd, debt_usd=debt_usd, equity_usd=equity_usd,
                           deposit_usd=deposit_usd, leverage=leverage, floor=floor)
    if not size.ok:
        options = [{"label": f"Use {size.max_leverage:.1f}x instead", "leverage": size.max_leverage}] if size.max_leverage else []
        tail = f" The most that can be done is about {size.max_leverage:.1f}x." if size.max_leverage else " There is no safe leverage available on this balance."
        return _no(size.reason + tail, options)

    borrow_amount = risk.usd_to_units(size.borrow_usd, p_borrow.usd, borrow_asset)
    if not float(borrow_amount) > 0:
        return _no("That leverage works out to a borrow too small to build.")

    # Pool liquidity is a hard gate the health maths cannot see.
    try:
        pool = await internal.call("vanna_earn_market", action="pool_stats", asset=borrow_asset)
        avail = risk.num(pool.get("available_liquidity"))
        if avail is not None and avail < float(borrow_amount):
            return _no(f"The {borrow_asset} pool only has {avail:,.4f} available to borrow, less than the {borrow_amount} this needs.")
    except ToolError as exc:
        return _no(humanize(exc))

    # The program's own borrow check, when the equity is already collateral (otherwise it runs live at that step).
    if has_account and deposit_units == 0:
        try:
            can = await internal.call("vanna_margin_status", action="can_borrow", authority=wallet, asset=borrow_asset, amount=borrow_amount)
            if can.get("allowed") is not True:
                return _no(f"The program would refuse that borrow: {can.get('reason') or 'it does not pass its checks'}.")
        except ToolError as exc:
            return _no(humanize(exc))

    try:
        quote = await internal.call("vanna_swap", action="quote", from_asset=borrow_asset, to_asset=asset,
                                    amount=borrow_amount, slippage_bps=risk.DEFAULT_SLIPPAGE_BPS)
        if risk.num(quote.get("out_amount")) is None:
            return _no("No usable swap quote came back, so this can't be planned.")
        expected_out = str(quote["out_amount"])
    except ToolError as exc:
        return _no(humanize(exc))

    steps: list[dict[str, Any]] = []
    if not has_account:
        steps.append(write_step("create_account"))
    if deposit_units > 0:
        steps.append(write_step("deposit", asset, risk.floor_decimals(deposit_units, risk.token_decimals(asset))))
    steps.append(write_step("borrow", borrow_asset, borrow_amount))
    steps.append(write_step("swap", borrow_asset, borrow_amount, to_asset=asset, mode="margin", slippage_bps=risk.DEFAULT_SLIPPAGE_BPS))
    steps.append(report_step())

    s = 1 - risk.DEFAULT_SLIPPAGE_BPS / 10_000
    hf_worst = size.hf_after
    summary = (
        f"Lever {asset} {leverage:g}x: {'deposit ' + risk.floor_decimals(deposit_units, 9) + ' ' + asset + ', ' if deposit_units > 0 else ''}"
        f"borrow {borrow_amount} {borrow_asset}, swap it into about {expected_out} {asset} inside your margin account. "
        f"Worst-case health factor {'no debt' if hf_worst is None else f'{hf_worst:.2f}'} (floor {floor:.2f})."
    )
    warnings = plan_warnings(steps, [
        f"Sized with the swap at its worst allowed price ({risk.DEFAULT_SLIPPAGE_BPS / 100:g}% slippage); health before: "
        f"{'no debt' if not health or health.hf is None else f'{health.hf:.2f}'}.",
        "Prices move between steps, so the borrow and the swap are each re-checked against your live health factor "
        "and pause if they would fall below the floor.",
        f"Leverage magnifies losses: if the price of {asset} falls your health factor falls with it, and at 1.10 your account can be liquidated.",
    ])
    simulation = {
        "hf_before": health.hf if health else None,
        "hf_after": hf_worst,
        "collateral_before": collateral_usd,
        "collateral_after": collateral_usd + deposit_usd + size.borrow_usd * s,
        "debt_before": debt_usd,
        "debt_after": debt_usd + size.borrow_usd,
        "liquidation_threshold": risk.LIQUIDATION_HF,
        "amount_usd": size.borrow_usd,
        "asset": borrow_asset,
        "margin_applicable": True,
    }
    return {"ok": True, "summary": summary, "steps": steps, "warnings": warnings, "simulation": simulation, "hf_floor": floor}


# ------------------------------------------------------------------ unwind


async def plan_unwind(wallet: str, scope: str = "all", close_account: bool = False) -> dict[str, Any]:
    """"Close everything": exit every position in an order where health can only improve at every step, so a plan
    stopped part-way (cancelled, or cut at the step cap) never leaves the account riskier than before:

      1. redeem every Kamino position back into the margin account (receipt -> underlying, value unchanged)
      2. repay each debt in full: from the margin balance when it covers it, else from the wallet, else first swap other
         margin collateral into the debt asset (sized by what must be received) and repay from margin
      3. withdraw each remaining collateral to the wallet (only after every debt step)
      4. redeem every Earn position
      5. close the margin account, only when asked
    Amounts are "all" and resolved against live balances when each step is built.
    """
    if scope not in ("all", "margin", "earn", "farm"):
        return _no("Scope must be one of: all, margin, earn, farm.")
    want_margin = scope in ("all", "margin")
    want_farm = scope in ("all", "margin", "farm")
    want_earn = scope in ("all", "earn")
    max_steps = get_settings().max_plan_steps

    h = await facts.load_health(wallet) if (want_margin or want_farm) else None
    farm: dict[str, Any] | Exception | None = None
    if want_farm:
        try:
            farm = await internal.call("vanna_farm_overview", action="positions", authority=wallet)
        except Exception as exc:  # noqa: BLE001
            farm = exc
    earn: list[tuple[str, dict[str, Any] | None]] = []
    if want_earn:
        for a in facts.LEND_BORROW_ASSETS:
            try:
                earn.append((a, await internal.call("vanna_earn_position", action="deposited_balance", owner=wallet, asset=a)))
            except Exception:  # noqa: BLE001
                earn.append((a, None))
    if h is not None and h.kind in ("unavailable", "malformed"):
        return _no("The margin account could not be read, so an exit plan won't be drawn from partial data. Try again in a moment.")
    health = h.health if h is not None and h.kind == "ok" else None

    steps: list[dict[str, Any]] = []
    notes: list[str] = []
    margin: dict[str, float] = {}
    for r in (health.collateral if health else []):
        if r.symbol.lower() not in _RECEIPTS and r.units > 0:
            margin[r.symbol] = r.units

    if want_farm and health and isinstance(farm, dict):
        for p in (farm.get("positions") if isinstance(farm.get("positions"), list) else []):
            symbol = p.get("symbol") if isinstance(p.get("symbol"), str) else ""
            underlying = _RECEIPTS.get(symbol.lower())
            receipt = risk.num(p.get("receipt_balance"))
            if not underlying or receipt is None or not receipt > 0:
                continue
            steps.append(write_step("farm_redeem", underlying, "all"))
            margin[underlying] = margin.get(underlying, 0.0) + (risk.num(p.get("underlying_amount")) or 0.0)

    if want_margin and health:
        for d in health.debt:
            if not d.units > 0:
                continue
            need = d.units * risk.DEBT_BUFFER
            held = margin.get(d.symbol, 0.0)
            if held >= need:
                steps.append(write_step("repay", d.symbol, "all", source="margin"))
                margin[d.symbol] = held - d.units
                continue
            in_wallet = (await facts.wallet_spendable(wallet, d.symbol)) or 0.0
            if in_wallet >= need:
                steps.append(write_step("repay", d.symbol, "all", source="wallet"))
                continue
            # Swap the shortfall in from the most valuable other margin collateral, then repay from margin.
            shortfall = (need - held) * 1.01
            price = d.price_usd or (d.usd / d.units if d.units > 0 else 0.0)
            candidates = []
            for sym, units in margin.items():
                if sym == d.symbol or not units > 0:
                    continue
                row = health.collateral_row(sym)
                px = (row.price_usd if row and row.price_usd else (row.usd / row.units if row and row.units > 0 else 0.0))
                candidates.append((sym, units, units * px))
            candidates.sort(key=lambda c: c[2], reverse=True)
            source = next((c for c in candidates if c[2] >= shortfall * price * 1.03), None)
            if source is None:
                return _no(
                    f"The {d.units_text} {d.symbol} debt can't be fully repaid from balances: the margin account holds "
                    f"{risk.floor_decimals(held, 9)} {d.symbol} and the wallet {risk.floor_decimals(in_wallet, 9)}. "
                    f"Add {d.symbol} to the wallet or margin account, then ask again."
                )
            target = risk.floor_decimals(shortfall, risk.token_decimals(d.symbol))
            steps.append(write_step("swap", source[0], f"{OUT_PREFIX}{target}", to_asset=d.symbol, mode="margin", slippage_bps=100))
            steps.append(write_step("repay", d.symbol, "all", source="margin"))
            notes.append(f"The margin account is short of {d.symbol} to repay in full, so {source[0]} collateral is swapped into about {target} {d.symbol} first.")
            margin[d.symbol] = 0.0
        for sym, units in margin.items():
            if units > risk.DUST_UNITS.get(sym, 1e-6):
                steps.append(write_step("withdraw", sym, "all"))

    if want_earn:
        for a, pos in earn:
            if pos and (risk.num(pos.get("share_balance")) or 0.0) > 0:
                steps.append(write_step("earn_redeem", a, "all"))

    if close_account and want_margin and health:
        steps.append(write_step("close_account"))

    if not steps:
        where = {"all": "", "earn": " in the Earn pools", "farm": " on Kamino", "margin": " in your margin account"}[scope]
        return _no(f"There are no open positions{where} to close.")

    kept = steps
    if len(steps) > max_steps:
        cut = max_steps
        if steps[cut - 1]["op"] == "swap":
            cut -= 1  # never cut between a swap and the repay it funds
        kept = steps[:cut]
        notes.append(f"Closing everything takes {len(steps)} steps; this plan covers the first {cut}. Ask again afterwards for the rest.")

    labels = ", then ".join(label_for(s)[0].lower() + label_for(s)[1:] for s in kept)
    return {
        "ok": True,
        "summary": f"Unwind: {labels}.",
        "steps": kept,
        "warnings": plan_warnings(kept, notes),
        "simulation": None,
        "hf_floor": get_settings().min_health_factor,
    }
