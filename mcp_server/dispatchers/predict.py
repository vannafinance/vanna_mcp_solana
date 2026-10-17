"""`vanna_predict` — World.xyz prediction markets on Vanna (the app's Predict page, vanna-credit-backend `1a9cb3c`).

Reads (markets, one event, the wallet's positions and trade history, a trade quote) and writes (a trade: buy, sell,
or move World shares into Vanna's pool, one or several outcomes; the keeper merge after it; the fork's CASH faucet).
The app builds each trade and its market maker co-signs it; the wallet signs every transaction of the batch in one
approval. Wallet-only: no margin account, no borrowing, no health factor.
"""

from __future__ import annotations

from typing import Any, Literal

from solders.pubkey import Pubkey

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

PredictAction = Literal["markets", "market", "positions", "history", "quote", "trade", "merge", "faucet"]


def _legs(legs: list[dict[str, Any]] | None, market: str | None, outcome: str | None, side: str | None,
          amount: str | None) -> list[dict[str, Any]]:
    if legs:
        return legs
    if market is None or outcome is None or amount is None:
        raise ValueError("Give 'legs', or 'market', 'outcome' and 'amount' (and 'side', default buy)")
    return [{"market": market, "outcome": outcome, "side": side or "buy", "amount": amount}]


@tool_safe
async def vanna_predict(
    action: PredictAction,
    authority: str | None = None,
    category: Literal["crypto", "sports", "politics", "economics", "other"] | None = None,
    query: str | None = None,
    id: str | None = None,
    market: str | None = None,
    outcome: Literal["yes", "no"] | None = None,
    side: Literal["buy", "sell", "deposit"] | None = None,
    amount: str | None = None,
    legs: list[dict[str, Any]] | None = None,
    market_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Prediction markets (World.xyz): YES / NO shares paid in CASH from the wallet. No margin, no health factor.

    A share pays 1 CASH if its outcome wins; prices are cents per share. Never say which side to take.

    Actions:
      markets — events and each outcome's market_id and prices (`category`, `query` to search).
      market — one event by `id` (event_id or market_id), with its rules.
      positions — the wallet's shares, value, P&L, CASH, and World shares it can move into Vanna's pool.
      history — past trades (optionally one `market`).
      quote — price a trade without building it. Always before trade.
      trade — `market` (a market_id from a read; never guess: ask when several match), `outcome` yes|no, `side`
        buy|sell|deposit, `amount`; or `legs` [{market, outcome, side, amount}] for up to 8 outcomes. All its
        transactions are signed in ONE approval. Afterwards call merge with summary.merge_markets.
      merge — `market_ids`. faucet — test CASH (test fork only; `amount` default 100, max 1000).
    Amounts: buy = CASH to spend (0.1-5,000). sell = shares; deposit = World shares moved into the pool. A sell or
    deposit may be "all" or "50%": "sell all my Senate shares" -> side="sell", amount="all" (the server reads the
    holding; never ask for the share count).
    authority: the wallet (omit when logged in).
    """
    ctx = get_context()
    predict = ctx.predict

    if action == "markets":
        return await predict.markets(category, query)
    if action == "market":
        if not id:
            return {"error": "invalid_input", "message": "'id' (an event_id or market_id) is required for action=market"}
        return await predict.market(id)
    if action == "merge":
        if not market_ids:
            return {"error": "invalid_input", "message": "'market_ids' is required for action=merge"}
        return await predict.merge(market_ids)

    if authority is None:
        return {"error": "invalid_input", "message": f"'authority' is required for action={action}"}
    owner = Pubkey.from_string(authority)

    if action == "positions":
        return await predict.positions(owner)
    if action == "history":
        return await predict.history(owner, market)
    if action == "quote":
        return await predict.quote(owner, _legs(legs, market, outcome, side, amount))
    if action == "trade":
        return (await predict.trade(owner, _legs(legs, market, outcome, side, amount))).to_dict()
    if action == "faucet":
        from mcp_server.safety import get_safety

        if not ctx.settings.enable_test_faucet:
            return {"error": "invalid_input", "message": "The test faucet is not enabled on this server (ENABLE_TEST_FAUCET)."}
        limit = await get_safety().consume_faucet(authority)
        if not limit.ok:
            return limit.to_error()
        return await predict.faucet(owner, amount)

    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_predict. Supported: markets, market, positions, history, quote, trade, merge, faucet",
    }
