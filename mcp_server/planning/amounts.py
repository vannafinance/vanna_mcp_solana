"""One amount vocabulary for every plan step: what a user says, resolved by this server into an exact amount.

The MCP client (Claude, Cursor, Codex …) never computes a number. It passes the user's words as one of these forms and
the server sizes them from live reads, the way the website Copilot's sizing kinds work (`lib/copilot/workflow/*`):

  exact      "10", "2.5"            the amount as typed (validated, never rounded)
  all        "all", "max", "max_safe"  the whole balance; for a borrow / withdraw the most the health floor (and the
                                       pool) allow. Resolved live when the step is built.
  percent    "50%"                  a share of the balance it applies to, resolved now
  usd        "usd:500", "$500"      that many dollars of the asset at the live price
  leverage   "leverage:3", "3x"     BORROW only: Vanna's leverage, collateral = N x the base. Borrows (N - 1) x the base's
                                       value and KEEPS it in the margin account; no swap. The base is this plan's earlier
                                       deposits, else the holding named by `base_asset` + `base_amount`, else the
                                       account's equity.
  health     "health:2"             borrow / withdraw / repay sized so the health factor lands on that value
  previous   "previous"             the amount of the step before (e.g. borrow, then supply what was borrowed)
  out        "out:100"              SWAP only: receive at least 100 of the asset bought

Every sized amount is still checked again when its step is built (floor, pool, limits, simulation).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from mcp_server.planning import facts
from mcp_server.planning.preflight import OUT_PREFIX, resolve_percent
from vanna_core import risk
from vanna_core.config import get_settings

ALL_WORDS = frozenset({"all", "max", "maximum", "max_safe", "safe_max", "everything"})
USD_PREFIX = "usd:"
LEVERAGE_PREFIX = "leverage:"
HEALTH_PREFIX = "health:"
PREVIOUS = "previous"


@dataclass
class Resolved:
    ok: bool
    amount: str | None = None
    message: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class Book:
    """A running estimate of the account while a plan is drawn: live collateral/debt plus the earlier steps' effect,
    so "deposit 10 SOL, then borrow 3x" or "… then borrow to health 2" sizes against the account after the deposit."""

    wallet: str
    collateral_usd: float = 0.0
    debt_usd: float = 0.0
    has_account: bool = False
    holdings: dict[str, float] = field(default_factory=dict)  # margin collateral units, live
    deposits: list[tuple[str, float]] = field(default_factory=list)  # (asset, units) deposited earlier in this plan
    prices: dict[str, float] = field(default_factory=dict)
    loaded: bool = False

    async def load(self) -> str | None:
        if self.loaded:
            return None
        self.loaded = True
        h = await facts.load_health(self.wallet)
        if h.kind in ("unavailable", "malformed"):
            return "The margin account could not be read just now, so this can't be sized. Try again in a moment."
        if h.kind == "ok" and h.health is not None:
            self.has_account = True
            self.collateral_usd, self.debt_usd = h.health.collateral_usd, h.health.debt_usd
            self.holdings = {r.symbol: r.units for r in h.health.collateral}
        return None

    async def price(self, asset: str) -> float | None:
        if asset not in self.prices:
            p = await facts.fresh_price(asset)
            if p is None:
                return None
            self.prices[asset] = p.usd
        return self.prices[asset]

    async def apply(self, step: dict[str, Any]) -> None:
        """Fold an earlier step with an exact amount into the estimate (live-resolved "all" steps are skipped)."""
        await self.load()  # the live figures first, or a later load would overwrite this step's effect
        op, asset, amount = step.get("op"), step.get("asset"), step.get("amount")
        try:
            units = float(amount) if isinstance(amount, str) else None
        except ValueError:
            units = None
        if not asset or units is None or not units > 0:
            return
        px = await self.price(asset)
        if px is None:
            return
        usd = units * px
        slots = step.get("slots") or {}
        if op == "deposit":
            self.collateral_usd += usd
            self.deposits.append((asset, units))
        elif op == "withdraw":
            self.collateral_usd = max(0.0, self.collateral_usd - usd)
        elif op == "borrow":
            self.collateral_usd += usd  # borrowed funds stay in the margin account
            self.debt_usd += usd
        elif op == "repay":
            self.debt_usd = max(0.0, self.debt_usd - usd)
            if slots.get("source") == "margin":
                self.collateral_usd = max(0.0, self.collateral_usd - usd)


def _floor_multiple(x: float) -> float:
    return math.floor(x * 100) / 100 if math.isfinite(x) else x


def _fmt_usd(x: float) -> str:
    return f"${x:,.2f}"


def parse_number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


async def size_leverage_borrow(book: Book, borrow_asset: str, multiple: float, *, base_asset: str | None = None,
                               base_amount: str | None = None, floor: float | None = None) -> Resolved:
    """Vanna's leverage (the website's and the Copilot's `sizeLevBorrow`): collateral / equity = N on a base B, reached by
    borrowing (N - 1) x B of value that stays in the margin account as collateral. No swap. Refused, with the highest
    multiple that does hold, when the floor, the per-action limit or the pool would not allow it."""
    settings = get_settings()
    floor = max(settings.min_health_factor, floor or 0.0)
    if not (multiple == multiple) or multiple <= 1:
        return Resolved(False, message="Leverage has to be above 1x.")
    if multiple > risk.MAX_LEVERAGE:
        return Resolved(False, message=f"Leverage above {risk.MAX_LEVERAGE:g}x can never hold the {floor:.2f} health floor.")
    if borrow_asset not in facts.LEND_BORROW_ASSETS:
        return Resolved(False, message=f"Only {', '.join(facts.LEND_BORROW_ASSETS)} can be borrowed.")
    problem = await book.load()
    if problem:
        return Resolved(False, message=problem)

    deposit_usd = 0.0
    for asset, units in book.deposits:
        px = await book.price(asset)
        if px is None:
            return Resolved(False, message=f"The {asset} price is unavailable right now, so this can't be sized safely. Try again in a minute.")
        deposit_usd += units * px
    # `book.collateral_usd` already includes this plan's earlier deposits; the leverage maths wants them apart.
    collateral_before = book.collateral_usd - deposit_usd

    if base_asset:
        want = parse_number(str(base_amount)) if base_amount is not None else None
        held = book.holdings.get(base_asset, 0.0) + sum(u for a, u in book.deposits if a == base_asset)
        if want is None:
            want = held
        if not want > 0 or held + 1e-9 < want:
            return Resolved(False, message=f"The leverage is taken on {base_amount or 'your'} {base_asset}, but your margin account holds "
                                           f"{risk.floor_decimals(held, 9)} {base_asset} (including this plan's deposits).")
        px = await book.price(base_asset)
        if px is None:
            return Resolved(False, message=f"The {base_asset} price is unavailable right now, so this can't be sized safely.")
        base_usd, basis = want * px, f"{risk.floor_decimals(want, 9)} {base_asset}"
    elif deposit_usd > 0:
        base_usd = deposit_usd
        basis = "your " + " + ".join(f"{risk.floor_decimals(u, 9)} {a}" for a, u in book.deposits) + " deposit"
    else:
        base_usd, basis = book.collateral_usd - book.debt_usd, "your account equity"
    if not base_usd > 0:
        return Resolved(False, message="There is nothing to take a leverage multiple of. Deposit something first, or give an exact amount to borrow.")

    size = risk.size_lever(collateral_usd=collateral_before, debt_usd=book.debt_usd, equity_usd=base_usd,
                           deposit_usd=deposit_usd, leverage=multiple, floor=floor, slippage_bps=0)
    if not size.ok:
        best = f" The most that holds the floor on {basis} is about {size.max_leverage:.1f}x." if size.max_leverage else " There is no room to borrow on it while keeping that floor."
        return Resolved(False, message=size.reason + best)
    borrow_usd = size.borrow_usd
    if borrow_usd > settings.max_action_usd:
        most = _floor_multiple(1 + settings.max_action_usd / base_usd)
        return Resolved(False, message=f"{multiple:g}x on {basis} borrows about {_fmt_usd(borrow_usd)}, above the per-action limit of "
                                       f"{_fmt_usd(settings.max_action_usd)}. The most in one action is about {most:.2f}x.")
    px_borrow = await book.price(borrow_asset)
    if px_borrow is None:
        return Resolved(False, message=f"The {borrow_asset} price is unavailable right now, so this can't be sized safely. Try again in a minute.")
    amount = risk.usd_to_units(borrow_usd, px_borrow, borrow_asset)
    if not float(amount) > 0:
        return Resolved(False, message=f"That leverage works out to less than the smallest {borrow_asset} amount.")
    available = await facts.pool_available(borrow_asset)
    if available is not None and float(amount) > available * facts.POOL_HEADROOM:
        most = _floor_multiple(1 + available * facts.POOL_HEADROOM * px_borrow / base_usd)
        tail = f" The most the pool allows on {basis} is about {most:.2f}x." if most > 1 else ""
        return Resolved(False, message=f"The {borrow_asset} pool has {available:,.4f} available to borrow, less than the {amount} this needs.{tail}")
    hf = "no debt" if size.hf_after is None else f"{size.hf_after:.2f}"
    return Resolved(True, amount, notes=[
        f"{multiple:g}x on {basis} ({_fmt_usd(base_usd)}): borrows {amount} {borrow_asset} (about {_fmt_usd(borrow_usd)}), "
        f"which stays in your margin account. Health factor after: {hf}.",
    ])


async def size_to_health(book: Book, op: str, asset: str, target: float, source: str | None = None) -> Resolved:
    """Borrow / withdraw / repay sized so the health factor lands on `target` (the Copilot's `sizeToHealth`)."""
    floor = get_settings().min_health_factor
    if not (target == target) or target <= 1:
        return Resolved(False, message="A target health factor has to be above 1.")
    if op in ("borrow", "withdraw") and target < floor:
        return Resolved(False, message=f"A borrow or withdrawal can't aim below the {floor:.2f} health floor.")
    problem = await book.load()
    if problem:
        return Resolved(False, message=problem)
    c, d = book.collateral_usd, book.debt_usd
    px = await book.price(asset)
    if px is None:
        return Resolved(False, message=f"The {asset} price is unavailable right now, so this can't be sized.")
    if op == "borrow":
        usd = (c - target * d) / (target - 1)  # (c + x) / (d + x) = target
    elif op == "withdraw":
        if not d > 0:
            return Resolved(False, message="With no debt there is no health factor to aim for; say how much to withdraw.")
        usd = c - target * d  # (c - x) / d = target
    elif op == "repay":
        if not d > 0:
            return Resolved(False, message="There is no debt to repay.")
        usd = (target * d - c) / (target - 1) if source == "margin" else d - c / target
    else:
        return Resolved(False, message=f"A target health factor applies to borrow, withdraw and repay, not {op}.")
    now = "no debt" if not d > 0 else f"{c / d:.2f}"
    if not usd > 0:
        return Resolved(False, message=f"Your health factor is {now} already, so there is nothing to {op} to reach {target:g}.")
    amount = risk.floor_decimals(usd / px, risk.token_decimals(asset))
    if not float(amount) > 0:
        return Resolved(False, message="That target works out to an amount too small to use.")
    return Resolved(True, amount, notes=[f"Sized so your health factor goes from {now} to about {target:g}: {amount} {asset} (about {_fmt_usd(usd)})."])


async def resolve(book: Book, op: str, asset: str | None, raw: Any, slots: dict[str, Any], earlier: list[dict[str, Any]],
                  step: dict[str, Any]) -> Resolved:
    """One step's amount in any vocabulary form -> an exact decimal, "all" (resolved live at build) or "out:N"."""
    if raw is None:
        return Resolved(True, None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = repr(raw) if isinstance(raw, float) else str(raw)
    if not isinstance(raw, str):
        return Resolved(False, message="An amount must be a string: a number, \"all\", \"50%\", \"usd:500\", \"leverage:3\", \"health:2\" or \"previous\".")
    text = raw.strip()
    low = text.lower()
    symbol = asset if asset in facts.PRODUCT_ASSETS else None

    if low in ALL_WORDS:
        return Resolved(True, "all")
    if low == PREVIOUS:
        prior = next((s for s in reversed(earlier) if s.get("kind", "write") == "write"), None)
        if prior is None or not prior.get("amount"):
            return Resolved(False, message='"previous" needs an earlier step with an amount.')
        if prior.get("amount") == "all" or str(prior.get("amount")).startswith(OUT_PREFIX):
            return Resolved(False, message='"previous" needs the earlier step to have an exact amount (it is resolved only when built).')
        if prior.get("asset") != asset:
            return Resolved(False, message=f'"previous" is {prior["amount"]} {prior.get("asset")}, but this step uses {asset}.')
        return Resolved(True, str(prior["amount"]))
    if low.startswith(OUT_PREFIX):
        if op != "swap":
            return Resolved(False, message='"out:N" (receive at least N) applies to swaps only.')
        to = slots.get("to_asset")
        parsed, issue = risk.parse_amount(text[len(OUT_PREFIX):], to if to in facts.PRODUCT_ASSETS else None)
        return Resolved(False, message=issue.message) if issue else Resolved(True, OUT_PREFIX + str(parsed))
    if low.endswith("%"):
        pct = parse_number(low[:-1].strip())
        if pct is None or not 0 < pct <= 100:
            return Resolved(False, message="A percentage must be between 0 and 100.")
        if pct >= 100:
            return Resolved(True, "all")
        r = await resolve_percent(book.wallet, op, str(asset), pct, margin=op == "swap" and slots.get("mode") == "margin")
        return Resolved(True, r["amount"]) if r["ok"] else Resolved(False, message=r["message"])
    if low.startswith(USD_PREFIX) or low.startswith("$"):
        usd = parse_number(low[len(USD_PREFIX):] if low.startswith(USD_PREFIX) else low[1:].replace(",", ""))
        if usd is None or not usd > 0:
            return Resolved(False, message="A dollar amount must be above 0, like \"usd:500\".")
        if not asset:
            return Resolved(False, message="A dollar amount needs the step's asset.")
        px = await book.price(asset)
        if px is None:
            return Resolved(False, message=f"The {asset} price is unavailable right now, so a dollar amount can't be converted.")
        amount = risk.floor_decimals(usd / px, risk.token_decimals(asset))
        if not float(amount) > 0:
            return Resolved(False, message="That dollar amount is too small for this asset.")
        return Resolved(True, amount, notes=[f"{_fmt_usd(usd)} of {asset} at the live price is {amount} {asset}."])
    lev = low[len(LEVERAGE_PREFIX):] if low.startswith(LEVERAGE_PREFIX) else (low[:-1] if low.endswith("x") else None)
    if lev is not None:
        multiple = parse_number(lev.strip())
        if multiple is None:
            return Resolved(False, message="A leverage amount looks like \"leverage:3\" (or \"3x\").")
        if op != "borrow":
            return Resolved(False, message="A leverage multiple sizes a borrow. Put it on the borrow step; the deposit is its own step with an exact amount.")
        return await size_leverage_borrow(book, str(asset), multiple, base_asset=step.get("base_asset"),
                                          base_amount=step.get("base_amount"), floor=step.get("min_hf"))
    if low.startswith(HEALTH_PREFIX):
        target = parse_number(low[len(HEALTH_PREFIX):].strip())
        if target is None:
            return Resolved(False, message="A target health factor looks like \"health:2\".")
        return await size_to_health(book, op, str(asset), target, slots.get("source"))
    parsed, issue = risk.parse_amount(text, symbol)
    return Resolved(False, message=issue.message) if issue else Resolved(True, parsed)
