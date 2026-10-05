"""Live reads shared by sizing, planning and pre-flight — a port of the Copilot's `lib/copilot/mcp-facts.ts`
(loadHealth, loadFreshPrice, sizeSafeBorrow, sizeSafeWithdraw) and `readHealth` / `readCan` from `risk.ts`.

Every figure here is read from this server's own tools; nothing is defaulted, and an unreadable input makes the
caller refuse rather than guess.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from mcp_server import internal
from mcp_server.internal import ToolError
from vanna_core import risk
from vanna_core.config import get_settings

LEND_BORROW_ASSETS = ("SOL", "USDC", "USDT")
COLLATERAL_ONLY_ASSETS = ("JitoSOL", "JupSOL")
PRODUCT_ASSETS = LEND_BORROW_ASSETS + COLLATERAL_ONLY_ASSETS
RECEIPT_TO_UNDERLYING = {"ksol": "SOL", "kusdc": "USDC", "kusdt": "USDT", "kjitosol": "JitoSOL", "kjupsol": "JupSOL"}


@dataclass
class PositionRow:
    symbol: str
    units: float
    usd: float
    price_usd: float | None
    units_text: str


@dataclass
class HealthRead:
    margin_account: str | None
    hf: float | None  # None = no debt
    collateral_usd: float
    debt_usd: float
    collateral: list[PositionRow] = field(default_factory=list)
    debt: list[PositionRow] = field(default_factory=list)
    is_liquidatable: bool = False
    prices_ok: bool = True

    def collateral_row(self, symbol: str) -> PositionRow | None:
        return next((r for r in self.collateral if r.symbol == symbol), None)

    def debt_row(self, symbol: str) -> PositionRow | None:
        return next((r for r in self.debt if r.symbol == symbol), None)


def _rows(raw: Any, units_key: str) -> list[PositionRow] | None:
    if not isinstance(raw, list):
        return None
    out: list[PositionRow] = []
    for r in raw:
        if not isinstance(r, dict):
            return None
        units, usd = risk.num(r.get(units_key)), risk.num(r.get("usd_value"))
        if not isinstance(r.get("symbol"), str) or units is None or usd is None:
            return None
        price = r.get("price")
        out.append(
            PositionRow(
                r["symbol"], units, usd,
                risk.num(price.get("usd")) if isinstance(price, dict) else None,
                str(r.get(units_key)),
            )
        )
    return out


def read_health(raw: dict[str, Any]) -> HealthRead | None:
    """Strict read of `vanna_account health`. None when any required field is missing or malformed."""
    if "health_factor" not in raw:
        return None
    collateral_usd, debt_usd = risk.num(raw.get("total_collateral_usd")), risk.num(raw.get("total_debt_usd"))
    collateral, debt = _rows(raw.get("collateral"), "ui_balance"), _rows(raw.get("debt"), "ui_debt")
    if collateral_usd is None or debt_usd is None or collateral is None or debt is None:
        return None
    hf: float | None = None
    if raw["health_factor"] is not None:
        hf = risk.num(raw["health_factor"])
        if hf is None:
            return None
    if hf is None and debt_usd > 0:
        return None
    return HealthRead(
        margin_account=raw.get("margin_account") if isinstance(raw.get("margin_account"), str) else None,
        hf=hf, collateral_usd=collateral_usd, debt_usd=debt_usd, collateral=collateral, debt=debt,
        is_liquidatable=raw.get("is_liquidatable") is True,
        prices_ok=raw.get("price_checks_pass") is not False,
    )


@dataclass
class CanResult:
    allowed: bool
    reason: str | None
    projected_hf: Any  # float | None (no debt afterwards) | risk.UNKNOWN (not reported)
    current_hf: Any


def read_can(raw: dict[str, Any]) -> CanResult | None:
    """The `allowed` / `projected_health_factor` of a can_borrow / can_withdraw / can_swap result."""
    if not isinstance(raw.get("allowed"), bool):
        return None

    def field_(key: str) -> Any:
        if key not in raw:
            return risk.UNKNOWN
        if raw[key] is None:
            return None
        n = risk.num(raw[key])
        return risk.UNKNOWN if n is None else n

    return CanResult(raw["allowed"], raw["reason"] if isinstance(raw.get("reason"), str) else None,
                     field_("projected_health_factor"), field_("current_health_factor"))


# ------------------------------------------------------------------ health / prices


@dataclass
class HealthResult:
    kind: str  # ok | no_account | unavailable | malformed
    health: HealthRead | None = None
    raw: dict[str, Any] | None = None
    error: Exception | None = None


async def load_health(wallet: str) -> HealthResult:
    try:
        raw = await internal.call("vanna_account", action="health", authority=wallet)
    except ToolError as exc:
        return HealthResult("no_account") if exc.code == "account_not_found" else HealthResult("unavailable", error=exc)
    except Exception as exc:  # noqa: BLE001
        return HealthResult("unavailable", error=exc)
    health = read_health(raw)
    return HealthResult("ok", health, raw) if health else HealthResult("malformed", raw=raw)


@dataclass
class PriceRead:
    usd: float
    stale: bool


async def load_price(asset: str) -> PriceRead | None:
    try:
        raw = await internal.call("vanna_oracle", action="price", asset=asset)
    except Exception:  # noqa: BLE001
        return None
    usd = risk.num(raw.get("price_usd"))
    if usd is None or not usd > 0:
        return None
    return PriceRead(usd, raw.get("stale") is True or raw.get("fresh") is False)


_last_refresh = 0.0
_REFRESH_MIN_GAP = 15.0


async def fresh_price(asset: str) -> PriceRead | None:
    """A price; if it is stale, refresh the fork's feeds once (throttled) and read again.

    A price still flagged stale afterwards is returned flagged, never guessed: the build is the authoritative check
    (it refreshes, simulates, and fails closed with StalePrice if a price is genuinely too old)."""
    global _last_refresh
    price = await load_price(asset)
    if price is not None and price.stale and time.monotonic() - _last_refresh >= _REFRESH_MIN_GAP:
        _last_refresh = time.monotonic()
        try:
            from mcp_server.dependencies import get_context
            from vanna_core.contracts.oracle_refresh import maybe_refresh_prices

            ctx = get_context()
            await maybe_refresh_prices(ctx.client, ctx.settings)
            price = await load_price(asset) or price
        except Exception:  # noqa: BLE001 — best effort
            pass
    return price


# ------------------------------------------------------------------ wallet


async def wallet_spendable(wallet: str, asset: str) -> float | None:
    """What the wallet can spend of `asset` (SOL keeps a fee/rent reserve)."""
    try:
        w = await internal.call("vanna_wallet", action="summary", authority=wallet)
    except Exception:  # noqa: BLE001
        return None
    if asset == "SOL":
        sol = risk.num(w.get("sol_balance"))
        return None if sol is None else max(0.0, sol - risk.SOL_FEE_RESERVE)
    rows = w.get("assets") if isinstance(w.get("assets"), list) else []
    row = next((a for a in rows if isinstance(a, dict) and a.get("symbol") == asset), None)
    return risk.num(row.get("wallet_balance")) if row else None


# ------------------------------------------------------------------ safe sizing


@dataclass
class Sized:
    ok: bool
    amount: str = "0"
    usd: float = 0.0
    hf_after: float | None = None
    protocol_max: str | None = None
    reason: str = ""


async def size_safe_borrow(wallet: str, asset: str, hint: HealthResult | None = None) -> Sized:
    """Largest borrow that keeps HF >= the floor, verified through the program's own `can_borrow`."""
    floor = get_settings().min_health_factor
    h = hint or await load_health(wallet)
    if h.kind == "no_account":
        return Sized(False, reason="You don't have a margin account yet.")
    if h.kind != "ok" or h.health is None:
        return Sized(False, reason="I couldn't read your account, so I can't size a safe borrow.")
    price = await fresh_price(asset)
    if price is None:
        return Sized(False, reason=f"The {asset} price is unavailable right now.")
    c, d = h.health.collateral_usd, h.health.debt_usd
    units = float(risk.usd_to_units(risk.max_borrow_usd(c, d, floor, True) * 0.995, price.usd, asset))
    for _ in range(8):
        if not units > 0:
            break
        amount = risk.floor_decimals(units, risk.token_decimals(asset))
        if not float(amount) > 0:
            break
        try:
            can = read_can(await internal.call("vanna_margin_status", action="can_borrow", authority=wallet, asset=asset, amount=amount))
        except ToolError as exc:
            return Sized(False, reason=exc.message)
        if can and can.allowed and isinstance(can.projected_hf, float) and can.projected_hf >= floor:
            protocol_max = None
            try:
                m = await internal.call("vanna_margin_status", action="max_borrow", authority=wallet, asset=asset)
                protocol_max = m.get("max_amount") if isinstance(m.get("max_amount"), str) else None
            except Exception:  # noqa: BLE001 — informational only
                pass
            return Sized(True, amount, float(amount) * price.usd, can.projected_hf, protocol_max)
        if can and not can.allowed and can.reason and "breach" not in can.reason.lower():
            return Sized(False, reason=can.reason)
        units *= 0.97
    return Sized(False, reason=f"There is no room to borrow {asset} while keeping your health factor at {floor:.2f} or above.")


async def size_safe_withdraw(wallet: str, asset: str, hint: HealthResult | None = None) -> Sized:
    """Largest collateral withdrawal that keeps HF >= the floor, verified through the program's own `can_withdraw`."""
    floor = get_settings().min_health_factor
    h = hint or await load_health(wallet)
    if h.kind == "no_account":
        return Sized(False, reason="You don't have a margin account yet.")
    if h.kind != "ok" or h.health is None:
        return Sized(False, reason="I couldn't read your account, so I can't size a safe withdrawal.")
    row = h.health.collateral_row(asset)
    if row is None or not row.units > 0:
        return Sized(False, reason=f"You have no {asset} collateral in your margin account.")
    price = row.price_usd or (lambda p: p.usd if p else None)(await load_price(asset))
    if not price:
        return Sized(False, reason=f"The {asset} price is unavailable right now.")
    c, d = h.health.collateral_usd, h.health.debt_usd
    limit_usd = risk.max_withdraw_usd(c, d, floor)
    units = row.units if d <= 0 else min(row.units, (limit_usd * 0.995) / price)
    for _ in range(8):
        if not units > 0:
            break
        amount = row.units_text if d <= 0 or units >= row.units else risk.floor_decimals(units, risk.token_decimals(asset))
        if not float(amount) > 0:
            break
        try:
            can = read_can(await internal.call("vanna_margin_status", action="can_withdraw", authority=wallet, asset=asset, amount=amount))
        except ToolError as exc:
            return Sized(False, reason=exc.message)
        hf_ok = can is not None and (can.projected_hf is None or (isinstance(can.projected_hf, float) and can.projected_hf >= floor))
        if can and can.allowed and hf_ok:
            projected = can.projected_hf if isinstance(can.projected_hf, float) else None
            return Sized(True, amount, float(amount) * price, projected)
        if can and not can.allowed and can.reason and "breach" not in can.reason.lower():
            return Sized(False, reason=can.reason)
        units *= 0.97
    return Sized(False, reason=f"You can't withdraw any {asset} right now without taking your health factor below {floor:.2f}.")
