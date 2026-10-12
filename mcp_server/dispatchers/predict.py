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
    """Prediction markets (World.xyz, on Vanna's test fork): browse markets, trade YES/NO shares for CASH, positions.

    A share of an outcome pays 1 CASH (World's dollar) if that outcome wins; prices are cents per share. Buying on a
    market that joins Vanna's pool deposits the shares into it automatically (they earn while held), as on the
    website. Payment is CASH from the wallet; Predict never uses the margin account and never changes the health factor.

    Args:
        action: "markets" — listed events with each outcome's market_id and buy/sell prices (filter by `category`,
            search with `query`). "market" — one event (`id`: event_id or market_id) with every outcome, pool figures
            and its rules. "positions" — the wallet's Predict positions (wallet + pooled shares, value, P&L, payout if
            win), its CASH, Vanna pool figures, and World shares it could move into the pool. "history" — the wallet's
            trades (optionally one `market`). "quote" — price a trade without building it. "trade" — build the trade
            for the wallet to sign: a batch of transactions, all signed in ONE approval and sent in order (each
            confirmed before the next); after it confirms call "merge" with summary.merge_markets. "merge" — the app's
            keeper merges matched YES+NO in those pools into USDC lent to Vanna (`market_ids`). "faucet" — TEST FORK
            ONLY: add test CASH to the wallet (`amount`, default 100, max 1000), the Predict page's "Get test CASH".
        authority: Base58 wallet address; required for positions, history, quote, trade, faucet.
        category: For "markets": crypto, sports, politics, economics or other.
        query: For "markets": search text (team, candidate, asset, league).
        id: For "market": an event_id or market_id.
        market: For one-leg "quote" / "trade": the outcome's market_id (from "markets"/"market"); for "history": a filter.
        outcome: "yes" or "no".
        side: "buy" (amount = CASH to spend, 0.1-5,000), "sell" (amount = shares; pooled shares are redeemed first
            when the wallet holds too few) or "deposit" (amount = World shares in the wallet to move into Vanna's pool).
        amount: Decimal string (up to 6 decimals).
        legs: For several outcomes in one trade (max 8): [{"market", "outcome", "side", "amount"}, ...].
        market_ids: For "merge".

    Returns: a dict; "trade" returns an unsigned (maker co-signed) batch envelope. On error: {"error", "message"}.
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
