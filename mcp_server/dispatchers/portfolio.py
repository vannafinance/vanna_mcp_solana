"""`vanna_portfolio` — the website's Portfolio page as one read: wallet, margin account, Earn deposits and Kamino
farm positions, with USD values, leverage, LTV and total net worth. Every figure is read from this server's own tools;
a part that cannot be read is named as missing, never estimated.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from mcp_server import internal
from mcp_server.error_handling import tool_safe
from mcp_server.internal import ToolError
from mcp_server.planning import facts
from vanna_core import risk

PortfolioAction = Literal["overview"]


async def _safe(coro: Any) -> Any:
    try:
        return await coro
    except ToolError as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        return exc


@tool_safe
async def vanna_portfolio(action: PortfolioAction, authority: str) -> dict[str, Any]:
    """Everything the user holds in Vanna on one page, with USD values.

    Args:
        action: "overview" — wallet tokens, the margin account (collateral, debt, equity, leverage = gross collateral /
            equity, LTV = debt / gross collateral, health factor), Earn deposits with redeemable amount, interest earned
            and pool APR, Kamino farm positions, and a net-worth total (wallet + margin equity + Earn + farm is NOT
            double counted: Kamino receipts are already margin collateral).
        authority: Base58 wallet address (optional when logged in with a wallet).

    On error: {"error": "invalid_input" | "contract_error", "message": "..."}
    """
    if action != "overview":
        return {"error": "invalid_input", "message": f"Unknown action {action!r} for vanna_portfolio. Supported: overview"}
    wallet_r, health_r, farm_r = await asyncio.gather(
        _safe(internal.call("vanna_wallet", action="summary", authority=authority)),
        _safe(facts.load_health(authority)),
        _safe(internal.call("vanna_farm_overview", action="positions", authority=authority)),
    )
    earn_r = await asyncio.gather(*[
        _safe(internal.call("vanna_earn_position", action="earned_interest", owner=authority, asset=a))
        for a in facts.LEND_BORROW_ASSETS
    ])
    pool_r = await asyncio.gather(*[
        _safe(internal.call("vanna_earn_market", action="pool_stats", asset=a)) for a in facts.LEND_BORROW_ASSETS
    ])
    gaps: list[str] = []

    wallet: dict[str, Any] | None = None
    wallet_usd = 0.0
    if isinstance(wallet_r, dict):
        held = [a for a in (wallet_r.get("assets") or []) if isinstance(a, dict) and (risk.num(a.get("wallet_balance")) or 0) > 0]
        prices: dict[str, Any] = {}
        symbols = [a["symbol"] for a in held if a["symbol"] in facts.PRODUCT_ASSETS]
        if "SOL" not in symbols:
            symbols.append("SOL")
        px = await _safe(internal.call("vanna_oracle", action="prices", assets=symbols))
        if isinstance(px, dict):
            prices = px
        tokens = []
        sol = risk.num(wallet_r.get("sol_balance")) or 0.0
        sol_px = risk.num((prices.get("SOL") or {}).get("price_usd")) if isinstance(prices.get("SOL"), dict) else None
        rows = [{"asset": "SOL (native)", "amount": wallet_r.get("sol_balance"), "usd": round(sol * sol_px, 2) if sol_px else None}]
        wallet_usd += sol * sol_px if sol_px else 0.0
        for a in held:
            sym, bal = a["symbol"], risk.num(a["wallet_balance"]) or 0.0
            p = risk.num((prices.get(sym) or {}).get("price_usd")) if isinstance(prices.get(sym), dict) else None
            usd = bal * p if p else None
            wallet_usd += usd or 0.0
            tokens.append({"asset": sym, "amount": a["wallet_balance"], "usd": round(usd, 2) if usd is not None else None})
        wallet = {"native_sol": wallet_r.get("sol_balance"), "tokens": rows[:1] + tokens, "usd": round(wallet_usd, 2)}
    else:
        gaps.append("wallet")

    margin: dict[str, Any] | None = None
    equity = 0.0
    if isinstance(health_r, facts.HealthResult) and health_r.kind == "ok" and health_r.health:
        h = health_r.health
        equity = h.collateral_usd - h.debt_usd
        margin = {
            "account": h.margin_account, "collateral_usd": round(h.collateral_usd, 2), "debt_usd": round(h.debt_usd, 2),
            "equity_usd": round(equity, 2), "health_factor": h.hf,
            "leverage": round(min(10.0, h.collateral_usd / equity), 2) if equity > 0 and h.debt_usd > 0 else 1.0,
            "ltv_pct": round(h.debt_usd / h.collateral_usd * 100, 2) if h.collateral_usd > 0 else 0.0,
            "collateral": [{"asset": r.symbol, "amount": r.units_text, "usd": round(r.usd, 2)} for r in h.collateral],
            "debt": [{"asset": r.symbol, "amount": r.units_text, "usd": round(r.usd, 2)} for r in h.debt],
        }
    elif isinstance(health_r, facts.HealthResult) and health_r.kind == "no_account":
        margin = {"account": None, "note": "No margin account yet."}
    else:
        gaps.append("margin account")

    earn: list[dict[str, Any]] = []
    earn_usd = 0.0
    for a, e, p in zip(facts.LEND_BORROW_ASSETS, earn_r, pool_r):
        if not isinstance(e, dict):
            continue
        redeemable = risk.num(e.get("redeemable_underlying")) or 0.0
        if redeemable <= 0:
            continue
        price = await facts.load_price(a)
        usd = redeemable * price.usd if price else None
        earn_usd += usd or 0.0
        earn.append({
            "pool": a, "redeemable": e.get("redeemable_underlying"), "shares": e.get("share_balance"),
            "interest_earned": e.get("earned_interest"), "interest_complete": e.get("complete"),
            "supply_apr_pct": p.get("supply_apr_pct") if isinstance(p, dict) else None,
            "usd": round(usd, 2) if usd is not None else None,
        })

    farm: list[dict[str, Any]] = []
    if isinstance(farm_r, dict):
        farm = [{"receipt": p.get("symbol"), "receipt_amount": p.get("receipt_balance"), "underlying": p.get("underlying_amount")}
                for p in (farm_r.get("positions") or []) if isinstance(p, dict)]
    elif not (isinstance(farm_r, ToolError) and farm_r.code == "account_not_found"):
        gaps.append("Kamino farm")

    return {
        "wallet": wallet, "margin": margin, "earn": earn, "farm": farm,
        "net_worth_usd": round(wallet_usd + equity + earn_usd, 2),
        "note": "Net worth = wallet + margin equity (collateral minus debt, Kamino receipts included) + Earn deposits. "
                "Prices are the program's own oracle.",
        **({"unavailable": gaps} if gaps else {}),
    }
