"""Predict: World.xyz prediction markets on Vanna's fork, through the app (vanna-credit-backend `lib/predict/*`).

The app's server is the source of truth and the builder. It lists World's events with live prices (JanusFI quotes,
else mainnet fills), reads the wallet's positions, and builds every trade as an ordered batch of v0 transactions that
the app's own market maker co-signs (`PREDICT_FORK_MAKER_SECRET_KEY`), so this module cannot build trades itself: it
calls the app (`/api/predict/*`, through the access gate) the way the website's Predict page does, checks what comes
back, and hands the batch to the user's wallet unchanged. It never signs.

Website parity (backend `1a9cb3c`, `aaed5cc`): buy YES / NO for CASH (auto-deposited into Vanna's outcome pool on
allowlisted markets), several outcomes in one trade (max 8), sell shares (pooled shares are redeemed first when the
wallet holds too few), move World shares already in the wallet into the pool ("deposit"), positions, trade history,
the keeper merge after a trade, and the fork's CASH faucet. Paying with USDC and skipping the pool (`noPool`) are not
offered, as on the website. Predict is wallet-only: no margin account, no borrowing, no health factor.

Batch safety, checked before anything is returned: every transaction is paid for by the user's wallet; its only other
signer is the app's maker (read from `/api/predict/status`), already signed; and it calls only the Predict programs
(World, the outcome pool, token, associated-token, compute budget, system). Anything else is refused.
"""

from __future__ import annotations

import base64
import logging
import time
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

import httpx
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from vanna_core.http_pool import pooled_http
from vanna_core.app_gate import app_request
from vanna_core.client import SolanaClient
from vanna_core.envelope import SimulationOutcome, TransactionEnvelope, WriteEnvelope
from vanna_core.types import ContractCallError

logger = logging.getLogger(__name__)

# lib/predict/constants.ts
WORLD_PROGRAM_ID = "prediCtPZCttYMvm2W3PtxmMxLmT1dtN7riU6Cxh6tM"
OUTCOME_POOL_PROGRAM_ID = "4kFzca1u9TUAVK7BjJZ5vVtfJ2QRATs38vN8Bq6qZwG3"
CASH_MINT = "CASHx9KJUStyftLFWGvEVf59SGeG9sh5FfcnZMVPCASH"
MIN_TRADE_CASH = Decimal("0.1")
MAX_TRADE_CASH = Decimal("5000")
MAX_TRADE_LEGS = 8
AMOUNT_DECIMALS = 6
# app/api/predict/faucet/route.ts (the Predict page's "Get test CASH" button adds 100).
FAUCET_DEFAULT_CASH = 100
FAUCET_MAX_CASH = 1000

ALLOWED_PROGRAMS = frozenset({
    WORLD_PROGRAM_ID,
    OUTCOME_POOL_PROGRAM_ID,
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022 (CASH, outcome tokens)
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # associated token accounts
    "ComputeBudget111111111111111111111111111111",
    "11111111111111111111111111111111",  # system (account creation)
})

SIDES = ("buy", "sell", "deposit")
OUTCOMES = ("yes", "no")
CATEGORIES = ("crypto", "sports", "politics", "economics", "other")

_MAKER_TTL_SECS = 600.0
_maker_cache: tuple[str, float] | None = None


def _cents(value: Any) -> float | None:
    return round(float(value), 2) if isinstance(value, (int, float)) else None


def _num(value: Any, digits: int = 6) -> float | None:
    return round(float(value), digits) if isinstance(value, (int, float)) else None


def parse_amount(value: Any, what: str) -> Decimal:
    """A positive decimal with at most 6 decimals (CASH and outcome tokens), never rounded."""
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{what} must be a number, got {value!r}") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{what} must be greater than 0")
    if -amount.as_tuple().exponent > AMOUNT_DECIMALS:
        raise ValueError(f"{what} has more than {AMOUNT_DECIMALS} decimals: {value}")
    return amount


def _relative_share(value: Any) -> Decimal | None:
    """"all" -> 1, "N%" (0 < N <= 100) -> N/100; anything else -> None (an exact amount, checked by `parse_amount`)."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in ("all", "max", "everything"):
        return Decimal(1)
    if text.endswith("%"):
        try:
            pct = Decimal(text[:-1].strip())
        except InvalidOperation:
            return None
        if pct.is_finite() and 0 < pct <= 100:
            return pct / 100
    return None


def validate_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The website's trade request legs, checked here first: 1-8 legs, yes/no, buy/sell/deposit, 0.1-5,000 CASH a buy."""
    if not isinstance(legs, list) or not legs:
        raise ValueError("At least one leg (market, outcome, side, amount) is required")
    if len(legs) > MAX_TRADE_LEGS:
        raise ValueError(f"At most {MAX_TRADE_LEGS} outcomes per trade")
    out = []
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            raise ValueError(f"leg {i + 1}: expected an object with market, outcome, side, amount")
        market = str(leg.get("market") or leg.get("market_id") or leg.get("marketId") or "").strip()
        try:
            Pubkey.from_string(market)
        except ValueError as exc:
            raise ValueError(f"leg {i + 1}: 'market' must be a market id from vanna_predict markets, got {market!r}") from exc
        outcome = str(leg.get("outcome") or "").lower()
        side = str(leg.get("side") or "buy").lower()
        if outcome not in OUTCOMES:
            raise ValueError(f"leg {i + 1}: 'outcome' must be yes or no")
        if side not in SIDES:
            raise ValueError(f"leg {i + 1}: 'side' must be buy, sell or deposit")
        amount = parse_amount(leg.get("amount"), f"leg {i + 1} amount")
        if side == "buy" and not MIN_TRADE_CASH <= amount <= MAX_TRADE_CASH:
            raise ValueError(f"leg {i + 1}: a buy is between {MIN_TRADE_CASH} and {MAX_TRADE_CASH:,} CASH")
        out.append({"marketId": market, "outcome": outcome, "side": side, "amount": float(amount)})
    return out


def _outcome(o: dict) -> dict:
    pool = o.get("poolDetail") or {}
    return {
        "market_id": o.get("marketId") or o.get("k"),
        "name": o.get("name"),
        "yes_label": o.get("yl") or "Yes",
        "no_label": o.get("nl") or "No",
        # Cents per share (1 share pays 1 CASH if it wins): what one costs to buy now, and what one sells for now.
        "yes_buy_cents": _cents(o.get("y")),
        "no_buy_cents": _cents(o.get("n")),
        "yes_sell_cents": _cents(o.get("yBid")),
        "no_sell_cents": _cents(o.get("nBid")),
        "tradable": bool(o.get("tradable")),
        "price_source": o.get("priceSource"),
        "joins_vanna_pool": bool(pool.get("allowlisted")),
        "open_interest_cash": _num(o.get("oi"), 2),
        "volume_24h_cash": _num(o.get("vol"), 2),
    }


def _event(e: dict, *, full: bool) -> dict:
    out = {
        "event_id": e.get("id"),
        "title": e.get("title"),
        "type": e.get("type"),
        "category": e.get("cat"),
        "league": e.get("tag") or e.get("league"),
        "ends": e.get("ends") or None,
        "live": bool(e.get("live")) or None,
        "last_trade": e.get("last") or None,
        "open_interest_cash": _num(e.get("oi"), 2),
        "volume_24h_cash": _num(e.get("v24"), 2),
        "one_outcome_wins": e.get("exh") if isinstance(e.get("exh"), bool) else None,
        "outcomes": [_outcome(o) for o in e.get("outs") or []],
    }
    if full:
        out.update({"rules": e.get("rules"), "note": e.get("note"), "source": e.get("source"), "score": e.get("score"),
                    "when": e.get("when")})
        for o, raw in zip(out["outcomes"], e.get("outs") or []):
            pool = raw.get("poolDetail") or {}
            o["pool"] = {"yes_supplied": _num((raw.get("pool") or {}).get("yes"), 2), "no_supplied": _num((raw.get("pool") or {}).get("no"), 2),
                         "usdc_lent_to_vanna": _num(pool.get("invested"), 2), "pending_merge": _num(pool.get("pendingMerge"), 2)}
    return {k: v for k, v in out.items() if v is not None}


def _quote(q: dict) -> dict:
    side = q.get("side")
    out = {
        "leg": (q.get("legIndex") or 0) + 1,
        "market_id": q.get("marketId"),
        "outcome": q.get("outcome"),
        "side": side,
        "cash_in" if side == "buy" else "shares_in": _num(q.get("amountIn")),
        "shares_out" if side == "buy" else "cash_out": _num(q.get("amountOut")) if side != "deposit" else None,
        "price_cents": _cents((q.get("price") or 0) * 100) if isinstance(q.get("price"), (int, float)) else None,
        "payout_if_win_cash": _num(q.get("payoutIfWin")),
        "shares_into_vanna_pool": _num(q.get("pooled")),
        "pool_shares_redeemed": _num(q.get("redeemedShares")),
    }
    return {k: v for k, v in out.items() if v is not None}


class PredictContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client
        self._settings = client.settings

    # ── the app ──────────────────────────────────────────────────────────────────────────────────────

    @property
    def base(self) -> str:
        s = self._settings
        if getattr(s, "predict_api_url", ""):
            return s.predict_api_url.rstrip("/")
        return f"{(s.perps_helper_url or s.price_keeper_url).rstrip('/')}/api/predict"

    async def _call(self, method: str, path: str, *, timeout: float = 60.0, **kwargs) -> dict:
        """One app call. The first events read after the app starts scans mainnet (10-40 s), so reads wait up to a minute."""
        try:
            async with pooled_http(timeout) as http:
                resp = await app_request(http, self._settings, method, f"{self.base}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            raise ContractCallError("Predict markets are still loading on the app (its first read scans mainnet). Try again in a minute.") from exc
        except httpx.HTTPError as exc:
            raise ContractCallError(f"The app's Predict service is unreachable: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code != 200 or not isinstance(body, dict):
            detail = body.get("error") if isinstance(body, dict) else None
            if resp.status_code == 401 and detail == "access_required":
                raise ContractCallError("The app refused the call (its access gate): APP_ACCESS_CODE is missing or wrong on this server.")
            raise ContractCallError(detail or f"Predict request failed (HTTP {resp.status_code})")
        return body

    async def maker(self) -> str:
        """The app's fork maker, the only co-signer a Predict trade may carry (GET /status), cached 10 minutes."""
        global _maker_cache
        if _maker_cache and _maker_cache[1] > time.monotonic():
            return _maker_cache[0]
        body = await self._call("GET", "/status", timeout=20.0)
        maker = body.get("maker")
        try:
            Pubkey.from_string(str(maker))
        except ValueError as exc:
            raise ContractCallError("The app did not name its Predict market maker, so no trade is built.") from exc
        _maker_cache = (str(maker), time.monotonic() + _MAKER_TTL_SECS)
        return str(maker)

    # ── reads ────────────────────────────────────────────────────────────────────────────────────────

    async def markets(self, category: str | None = None, query: str | None = None) -> dict:
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
        params = {k: v for k, v in (("cat", category), ("q", (query or "").strip() or None)) if v}
        body = await self._call("GET", "/events", params=params, timeout=90.0)
        events = [_event(e, full=False) for e in body.get("events") or []]
        return {
            "events": events,
            "count": len(events),
            "note": ("Prices are cents per share; a share pays 1 CASH if its outcome wins (CASH is World's dollar). 'buy' is "
                     "what one costs now, 'sell' what one sells for now. An outcome that is not tradable has no live price. "
                     "A buy on a market that joins Vanna's pool is deposited into it automatically, as on the website. "
                     "Use the market_id of an outcome to quote or trade. Predict uses the wallet, not the margin account."),
        }

    async def market(self, market_or_event_id: str) -> dict:
        ident = str(market_or_event_id or "").strip()
        if not ident or "/" in ident or len(ident) > 64:
            raise ValueError("Give an event_id or a market_id from vanna_predict markets")
        body = await self._call("GET", f"/events/{ident}", timeout=90.0)
        event = body.get("event")
        if not isinstance(event, dict):
            raise ContractCallError("Event not found")
        return {"event": _event(event, full=True)}

    async def positions(self, owner: Pubkey) -> dict:
        body = await self._call("GET", f"/positions/{owner}", timeout=90.0)
        wallet = body.get("wallet") or {}
        summary = body.get("summary") or {}
        vault = body.get("vault") or {}
        return {
            "owner": str(owner),
            "wallet": {"cash": _num(wallet.get("cash")), "usdc": _num(wallet.get("usdc")), "sol": _num(wallet.get("sol"), 9)},
            "positions": [
                {
                    "event": p.get("event"), "event_id": p.get("eventId"), "market_id": p.get("marketId"),
                    "outcome": p.get("outcome"), "side": p.get("side"), "label": p.get("label"),
                    "shares": _num(p.get("shares")), "wallet_shares": _num(p.get("walletShares")),
                    "pooled_shares": _num(p.get("poolValueShares")),
                    "avg_price_cents": _cents((p.get("avg") or 0) * 100) if isinstance(p.get("avg"), (int, float)) else None,
                    "now_price_cents": _cents((p.get("mark") or 0) * 100) if isinstance(p.get("mark"), (int, float)) else None,
                    "value_usd": _num(p.get("value"), 2), "cost_usd": _num(p.get("cost"), 2), "pnl_usd": _num(p.get("pnl"), 2),
                    "pays_if_win_cash": _num(p.get("pays"), 2), "pool_apy_pct": _num(p.get("apy"), 2),
                    "matched_pct": _num(p.get("matched"), 2), "merging": bool(p.get("merging")), "source": p.get("src"),
                }
                for p in body.get("positions") or []
            ],
            "summary": {"value_usd": _num(summary.get("value"), 2), "cost_usd": _num(summary.get("cost"), 2),
                        "pnl_usd": _num(summary.get("pnl"), 2), "payout_if_all_win_cash": _num(summary.get("payout"), 2),
                        "in_vanna_pool_usd": _num(summary.get("inVault"), 2), "pool_apy_pct": _num(summary.get("apy"), 2),
                        "count": summary.get("count")},
            "vanna_pool": {"deposited_usd": _num(vault.get("deposited"), 2), "apy_pct": _num(vault.get("apy"), 2),
                           "matched_pairs": _num(vault.get("pairs"), 2), "events": vault.get("events")},
            # World shares already in the wallet that can be moved into Vanna's pool (action=trade, side=deposit).
            "can_move_to_pool": [
                {"event": x.get("event"), "market_id": x.get("marketId"), "outcome": x.get("side"), "label": x.get("label"),
                 "shares": _num(x.get("shares")), "value_usd": _num(x.get("value"), 2), "pool_apy_pct": _num(x.get("apy"), 2)}
                for x in body.get("importable") or [] if x.get("marketId") and x.get("listed", True)
            ],
        }

    async def history(self, owner: Pubkey, market_id: str | None = None) -> dict:
        params = {"market": market_id} if market_id else None
        body = await self._call("GET", f"/activity/{owner}", params=params, timeout=60.0)
        trades = [
            {"signature": t.get("signature"),
             "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t["time"] / 1000)) if isinstance(t.get("time"), (int, float)) else None,
             "question": t.get("question"), "market_id": t.get("marketId"), "event_id": t.get("eventId"),
             "outcome": t.get("outcomeLabel") or t.get("outcome"), "side": t.get("side"), "shares": _num(t.get("shares")),
             "cash": _num(t.get("cash")), "price_cents": _cents((t.get("price") or 0) * 100) if isinstance(t.get("price"), (int, float)) else None,
             "pooled_shares": _num(t.get("pooled"))}
            for t in body.get("trades") or []
        ]
        return {"owner": str(owner), "trades": trades, "count": len(trades)}

    # ── trades ───────────────────────────────────────────────────────────────────────────────────────

    async def resolve_relative_amounts(self, owner: Pubkey, legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """"Sell all", "sell 50%", "move all to the pool": a sell or deposit leg's `amount` may be "all" or "N%", resolved
        here against the wallet's live holding of that outcome (sell: every share it holds, pooled ones included, the
        way the website sells; deposit: the World shares in the wallet that can move into the pool). Buys stay CASH
        amounts. Legs with exact amounts pass through untouched."""
        relative = [i for i, leg in enumerate(legs) if isinstance(leg, dict) and _relative_share(leg.get("amount")) is not None]
        if not relative:
            return legs
        held = await self.positions(owner)
        out = [dict(leg) if isinstance(leg, dict) else leg for leg in legs]
        for i in relative:
            leg = out[i]
            side = str(leg.get("side") or "buy").lower()
            if side == "buy":
                raise ValueError(f"leg {i + 1}: a buy is an amount of CASH to spend; 'all' and percentages apply to sells and pool deposits")
            market = str(leg.get("market") or leg.get("market_id") or leg.get("marketId") or "").strip()
            outcome = str(leg.get("outcome") or "").lower()
            if side == "sell":
                rows = [p for p in held["positions"] if p.get("market_id") == market
                        and outcome in (str(p.get("side") or "").lower(), str(p.get("outcome") or "").lower())]
                have = sum(p.get("shares") or 0 for p in rows)
            else:
                rows = [p for p in held["can_move_to_pool"] if p.get("market_id") == market and str(p.get("outcome") or "").lower() == outcome]
                have = sum(p.get("shares") or 0 for p in rows)
            if not have > 0:
                where = "holds no" if side == "sell" else "has no World"
                raise ValueError(f"leg {i + 1}: the wallet {where} {outcome.upper()} shares of that market to {'sell' if side == 'sell' else 'move'}")
            share = _relative_share(leg.get("amount"))
            quant = Decimal(1).scaleb(-AMOUNT_DECIMALS)
            amount = (Decimal(str(have)) * share).quantize(quant, rounding=ROUND_DOWN)
            if not amount > 0:
                raise ValueError(f"leg {i + 1}: that percentage of the {have} shares held is too small to trade")
            leg["amount"] = format(amount.normalize(), "f")
        return out

    async def _build(self, owner: Pubkey, legs: list[dict[str, Any]]) -> dict:
        legs = await self.resolve_relative_amounts(owner, legs)
        # CASH only and always into the pool where it is allowlisted, as the website's Predict page sends it.
        return await self._call("POST", "/trade", json={"owner": str(owner), "legs": validate_legs(legs), "pay": "CASH"}, timeout=90.0)

    async def quote(self, owner: Pubkey, legs: list[dict[str, Any]]) -> dict:
        """What a trade would do at the app's live prices: nothing is returned to sign."""
        body = await self._build(owner, legs)
        quotes = [_quote(q) for q in body.get("quotes") or []]
        return {
            "owner": str(owner),
            "quotes": quotes,
            "cash_spent": _num(sum(q.get("cash_in") or 0 for q in quotes)),
            "transactions": len(body.get("transactions") or []),
            "warnings": list(body.get("warnings") or []),
            "note": "A quote, not a trade: prices move, and the trade is priced again when it is built for signing.",
        }

    def check_batch(self, owner: Pubkey, maker: str, built: list[dict]) -> list[VersionedTransaction]:
        """Every transaction: paid by the owner, co-signed by nobody but the app's maker (already signed), Predict programs only."""
        if not built:
            raise ContractCallError("The app returned no transactions for this trade.")
        txs = []
        for i, entry in enumerate(built):
            try:
                tx = VersionedTransaction.from_bytes(base64.b64decode(entry["transaction"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ContractCallError(f"Transaction {i + 1} from the app is not a valid transaction.") from exc
            message = tx.message
            keys = list(message.account_keys)
            required = message.header.num_required_signatures
            signers = [str(k) for k in keys[:required]]
            if not signers or signers[0] != str(owner):
                raise ContractCallError(f"Transaction {i + 1} is not paid for by your wallet, so it was refused.")
            for signer, signature in zip(signers[1:], list(tx.signatures)[1:]):
                if signer != maker:
                    raise ContractCallError(f"Transaction {i + 1} asks for a signer other than your wallet and the app's maker, so it was refused.")
                if signature == Signature.default():
                    raise ContractCallError(f"Transaction {i + 1} is missing the app maker's signature.")
            if message.address_table_lookups and any(ix.program_id_index >= len(keys) for ix in message.instructions):
                raise ContractCallError(f"Transaction {i + 1} loads a program from a lookup table, so it was refused.")
            programs = {str(keys[ix.program_id_index]) for ix in message.instructions}
            unknown = programs - ALLOWED_PROGRAMS
            if unknown:
                raise ContractCallError(f"Transaction {i + 1} calls a program outside Predict ({', '.join(sorted(unknown))}), so it was refused.")
            txs.append(tx)
        return txs

    async def trade(self, owner: Pubkey, legs: list[dict[str, Any]]) -> WriteEnvelope:
        """The website's trade as one batch for one wallet approval: sign every transaction together, send them in order."""
        maker = await self.maker()
        body = await self._build(owner, legs)
        built = body.get("transactions") or []
        txs = self.check_batch(owner, maker, built)
        last_valid = int(body.get("lastValidBlockHeight") or 0)

        # The first transaction is simulated; each later one spends what the one before it creates, so it can only run
        # after it (its simulation is "not_run_dependency"), and the network checks it when it is sent.
        sim = await self._client.simulate_versioned_transaction(txs[0])
        ok = sim.value.err is None
        err = None if ok else f"{sim.value.err}\n" + "\n".join((sim.value.logs or [])[-6:])
        first = SimulationOutcome(status="success" if ok else "failed", err=err, logs=list(sim.value.logs or []),
                                  units_consumed=sim.value.units_consumed, context_slot=sim.context.slot if sim.context else None)
        entries = []
        for i, (entry, tx) in enumerate(zip(built, txs)):
            signers = [str(k) for k in list(tx.message.account_keys)[: tx.message.header.num_required_signatures]]
            entries.append(TransactionEnvelope(
                id=f"predict-{i + 1}",
                purpose=str(entry.get("label") or f"Predict transaction {i + 1}"),
                depends_on=[f"predict-{i}"] if i else [],
                # Partly signed: the app's maker signature is already in it; the wallet adds the owner's.
                unsigned_transaction=entry["transaction"],
                blockhash=str(tx.message.recent_blockhash),
                last_valid_block_height=str(last_valid),
                required_signers=signers,
                address_lookup_tables=[str(t.account_key) for t in tx.message.address_table_lookups],
                simulation=first if i == 0 else SimulationOutcome(status="not_run_dependency"),
            ))
        quotes = [_quote(q) for q in body.get("quotes") or []]
        merge = [m for m in body.get("mergeMarkets") or [] if isinstance(m, str)]
        summary = {
            "action": "predict_trade",
            "quotes": quotes,
            "cash_spent": _num(sum(q.get("cash_in") or 0 for q in quotes)),
            "warnings": list(body.get("warnings") or []),
            "batch": True,
            "sign_all_at_once": True,
            "send_in_order": True,
            "co_signer": maker,
            "merge_markets": merge,
            "after_confirm": {"tool": "vanna_predict", "action": "merge", "market_ids": merge} if merge else None,
            "note": ("Sign every transaction together in one wallet approval, then send them in order, each confirmed "
                     "before the next. Predict uses your wallet's CASH, not the margin account; it never borrows."),
        }
        return WriteEnvelope(
            cluster=self._settings.solana_cluster_label,
            program_id=WORLD_PROGRAM_ID,
            fee_payer=str(owner),
            transactions=entries,
            summary=summary,
            simulation_success=ok,
            code=None if ok else "simulation_failed",
            reason=err,
        )

    async def merge(self, market_ids: list[str]) -> dict:
        """The app's keeper merges each pool's matched YES+NO into USDC lent to Vanna (after a trade confirms). Harmless to repeat."""
        ids = [str(m).strip() for m in market_ids or [] if str(m).strip()]
        if not 1 <= len(ids) <= 10:
            raise ValueError("market_ids: 1 to 10 market ids")
        for m in ids:
            Pubkey.from_string(m)
        body = await self._call("POST", "/pool/merge", json={"marketIds": ids}, timeout=120.0)
        return {"results": [
            {"market_id": r.get("marketId"), "merged_usdc": _num(r.get("merged")), "signature": r.get("signature"),
             "skipped": r.get("skipped"), "error": r.get("error")}
            for r in body.get("results") or []
        ]}

    async def faucet(self, owner: Pubkey, amount: Any = None) -> dict:
        """Test fork only: the Predict page's "Get test CASH" (+CASH and a little SOL for fees, a cheatcode, not a transaction)."""
        if "fork" not in self._settings.solana_cluster_label.lower():
            raise ContractCallError("The CASH faucet only exists on the test fork.")
        cash = FAUCET_DEFAULT_CASH if amount in (None, "") else parse_amount(amount, "amount")
        if Decimal(str(cash)) > FAUCET_MAX_CASH:
            raise ValueError(f"The CASH faucet adds at most {FAUCET_MAX_CASH} CASH at a time")
        body = await self._call("POST", "/faucet", json={"address": str(owner), "amount": float(cash)}, timeout=60.0)
        return {"ok": True, "added_cash": _num(body.get("added")), "cash": _num(body.get("cash")), "sol": _num(body.get("sol"), 9),
                "note": "Test CASH on the fork (a cheatcode, not a transaction)."}
