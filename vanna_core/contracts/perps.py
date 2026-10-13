"""Phoenix perps reads: markets, a margin's positions, and an order preview.

A port of the app's `lib/solana/perps.ts` (`fetchPerpsView`, `toMarketInfo`, and `openPerp`'s sizing and checks) at
vanna-credit-backend main `ef7bebc`. Read-only: nothing here builds a transaction. Perps leverage is Phoenix's own;
opening a position never borrows from Vanna (the margin's USDC moves into Phoenix as collateral).

Liquidation price (the app's formula): the margin becomes liquidatable when its holdings + the position's change in
equity = 1.10 x its Vanna debt, with only this market moving; no debt, or a price past the venue's zero-equity point,
means none.
"""

from __future__ import annotations

import logging

from solders.pubkey import Pubkey

from vanna_core.http_pool import pooled_http
from vanna_core import math_utils
from vanna_core.client import SolanaClient
from vanna_core.risk import LIQUIDATION_HF
from vanna_core.phoenix import (
    FULL_TRADER_CAPABILITIES,
    ListedMarket,
    PhoenixVenue,
    load_phoenix_venue,
    lots_to_tokens,
)
from vanna_core.types import AccountNotFoundError

logger = logging.getLogger(__name__)

# Default worst-price protection for market orders, bps: 1% on crypto, 2% on the stock markets (thinner books).
DEFAULT_SLIPPAGE_BPS = 100
STOCK_SLIPPAGE_BPS = 200
STOCK_MARKETS = frozenset({"TSLA", "META"})
_NANO = 1e9


def default_slippage_bps(market: str) -> int:
    return STOCK_SLIPPAGE_BPS if market_key(market) in STOCK_MARKETS else DEFAULT_SLIPPAGE_BPS


def market_key(market: str) -> str:
    """"sol", "SOL-PERP", "sol-perp" -> "SOL"."""
    key = market.strip().upper()
    return key[: -len("-PERP")] if key.endswith("-PERP") else key


def _num(x: float | None, places: int) -> str | None:
    return None if x is None else f"{x:.{places}f}"


def _market_info(m: ListedMarket) -> dict:
    fee_micro = m.orderbook.taker_fee_micro if m.orderbook else None
    return {
        "market": m.meta.key,
        "name": m.meta.name,
        "index_price": _num(m.index_usd, 6),
        "mark_price": _num(m.mark_usd, 6),
        "price_fresh": m.index_fresh,
        "max_leverage": m.entry.max_leverage_bps / 10_000,
        "trading_enabled": m.entry.trading_enabled,
        "taker_fee_pct": None if fee_micro is None else f"{fee_micro / 1e4:.4f}",
        "default_slippage_bps": default_slippage_bps(m.meta.key),
    }


def ref_price(m: ListedMarket, live_mark: float | None = None) -> float:
    """What an order's limit is set around: where Phoenix's book trades (the fork's refreshed mainnet mark when known,
    else the market's own mark), else the index."""
    if live_mark and live_mark > 0:
        return live_mark
    if m.mark_usd > 0:
        return m.mark_usd
    return m.index_usd or 0.0


def _out_of_compute(envelope) -> bool:
    """A simulation that failed because the transaction ran out of compute (not because the program refused it)."""
    for tx in envelope.transactions:
        sim = tx.simulation
        if sim.status == "success":
            continue
        text = "\n".join([sim.err or "", *sim.logs])
        if "exceeded CUs meter" in text or "ComputationalBudgetExceeded" in text:
            return True
    return False


class PerpsContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client

    async def _venue(self, margin: Pubkey) -> PhoenixVenue:
        venue = await load_phoenix_venue(self._client, margin, await self._client.get_chain_clock_unix_timestamp())
        if venue is None:
            raise ValueError("Phoenix markets aren't listed on this network yet")
        return venue

    async def markets(self) -> dict:
        venue = await self._venue(Pubkey.default())
        return {"venue": "Phoenix", "markets": [_market_info(m) for m in venue.markets]}

    async def _account(self, authority: Pubkey):
        """(snapshot, venue) for a margin, or (None, venue) when it has no margin account."""
        from vanna_core.contracts.margin import MarginContract

        margin_contract = MarginContract(self._client)
        try:
            snap = await margin_contract._snapshot(authority)
        except AccountNotFoundError:
            snap = None
        venue = await self._venue(margin_contract.margin_address(authority))
        return snap, venue

    async def positions(self, authority: Pubkey) -> dict:
        snap, venue = await self._account(authority)
        if snap is None:
            return {"has_margin_account": False, "has_phoenix_account": False, "markets": [_market_info(m) for m in venue.markets], "positions": []}
        holdings = snap.health.total_collateral_value / _NANO
        debt = snap.health.total_debt_value / _NANO
        venue_value = venue.value_nano / _NANO
        exposure = sum(p.size_usd for p in venue.positions)
        liq_hf = LIQUIDATION_HF
        usdc_row = next((r for r in snap.collateral_rows if r["symbol"] == "USDC"), None)
        rows = []
        for p in venue.positions:
            zero_equity = (p.index_price - venue_value / p.size_tokens if p.is_long else p.index_price + venue_value / p.size_tokens) if p.size_tokens > 0 else 0.0
            liq = None
            if debt > 0 and p.size_tokens > 0:
                room = (holdings - liq_hf * debt) / p.size_tokens
                candidate = p.index_price - room if p.is_long else p.index_price + room
                reachable = candidate > zero_equity if p.is_long else candidate < zero_equity
                liq = candidate if reachable and candidate > 0 else None
            rows.append({
                "market": p.key,
                "name": p.name,
                "side": "long" if p.is_long else "short",
                "size": _num(p.size_tokens, 6),
                "size_usd": _num(p.size_usd, 2),
                "entry_price": _num(p.entry_price, 6),
                "index_price": _num(p.index_price, 6),
                "mark_price": _num(p.mark_price, 6),
                "pnl_usd": _num(p.pnl_usd, 2),
                "pnl_pct": _num(p.pnl_usd / (p.entry_price * p.size_tokens) * 100 if p.entry_price > 0 and p.size_tokens > 0 else 0.0, 2),
                "funding_usd": _num(p.funding_usd, 2),
                "close_fee_usd": _num(p.close_fee_usd, 2),
                "equity_usd": _num(p.equity_usd, 2),
                "zero_equity_price": _num(max(0.0, zero_equity), 6),
                "liquidation_price": _num(liq, 6),
            })
        return {
            "has_margin_account": True,
            "has_phoenix_account": venue.trader is not None,
            "venue": "Phoenix",
            "margin_usdc": usdc_row["ui_balance"] if usdc_row else "0",
            "phoenix_value_usd": _num(venue_value, 2),
            "phoenix_collateral_usdc": _num(venue.collateral_usd, 2),
            "withdrawable_now_usdc": _num(venue.withdrawable_now / 1e6, 6),
            "leverage": _num(exposure / venue_value if venue_value > 0 else 0.0, 2),
            "total_collateral_usd": math_utils.usd_value_ui(snap.health.total_collateral_value),
            "total_debt_usd": math_utils.usd_value_ui(snap.health.total_debt_value),
            "health_factor": snap.health.health_factor_str,
            "positions": rows,
        }

    async def preview(
        self,
        authority: Pubkey,
        market: str,
        side: str,
        collateral_usdc: str,
        leverage: float,
        slippage_bps: int | None = None,
        funded: bool = False,
    ) -> dict:
        """What opening (or adding to) a position would do, with the app's own checks and sizing (`openPerp`):
        `collateral_usdc` of margin USDC moves into Phoenix ("0" = use the Phoenix balance already there) and a market
        order of collateral x leverage USD is sized at the limit price, kept inside the market's leverage cap together
        with the other markets' exposure. Nothing is built. `funded`: the USDC is already in Phoenix (see `open`)."""
        snap, venue = await self._account(authority)
        sized = self._size(snap, venue, market, side, collateral_usdc, leverage, slippage_bps, funded=funded)
        return {k: v for k, v in sized.items() if not k.startswith("_")}

    def _size(
        self, snap, venue: PhoenixVenue, market: str, side: str, collateral_usdc: str, leverage: float,
        slippage_bps: int | None, live_mark: float | None = None, funded: bool = False,
    ) -> dict:
        """The app's `openPerp` checks and sizing. Keys starting with "_" carry what the build needs.
        `funded`: `collateral_usdc` is already in Phoenix (an earlier funding transaction of this same open moved it):
        the order is sized from it and nothing more moves."""
        side = side.strip().lower()
        if side not in ("long", "short"):
            return {"allowed": False, "reason": "side must be long or short"}
        if snap is None:
            return {"allowed": False, "reason": "Create a margin account first"}
        key = market_key(market)
        m = next((x for x in venue.markets if x.meta.key == key), None)
        if m is None:
            return {"allowed": False, "reason": f"{key}-PERP is not a listed Phoenix market. Markets: {', '.join(x.meta.key for x in venue.markets)}"}
        max_lev = m.entry.max_leverage_bps / 10_000
        base = {"market": m.meta.key, "name": m.meta.name, "side": side, "max_leverage": max_lev}
        if not m.entry.trading_enabled:
            return {**base, "allowed": False, "reason": f"{m.meta.name} is closed for new positions"}
        if not (1 <= leverage <= max_lev):
            return {**base, "allowed": False, "reason": f"Leverage must be between 1x and {max_lev:g}x"}
        if venue.trader and (venue.trader.hot or venue.trader.withdraw_queued):
            return {**base, "allowed": False, "reason": "This Phoenix account has a queued withdrawal; try again shortly"}
        from decimal import Decimal, InvalidOperation

        try:
            requested = Decimal(str(collateral_usdc).strip())
        except InvalidOperation:
            requested = Decimal(-1)
        if not requested.is_finite() or requested < 0:
            return {**base, "allowed": False, "reason": "collateral_usdc must be a decimal amount of USDC (0 uses the Phoenix balance)"}
        try:
            amount = 0 if requested == 0 else math_utils.base_units(collateral_usdc, 6)
        except Exception:  # noqa: BLE001 - more decimals than USDC has, etc.
            return {**base, "allowed": False, "reason": "collateral_usdc must be a decimal amount of USDC with at most 6 decimals"}
        in_phoenix = venue.trader.collateral if venue.trader else 0
        if funded:
            if amount > in_phoenix:
                return {**base, "allowed": False, "reason": f"Only {in_phoenix / 1e6:g} USDC is in Phoenix, less than the {amount / 1e6:g} this order was funded with"}
            deposit = 0
        else:
            usdc_row = next((r for r in snap.collateral_rows if r["symbol"] == "USDC"), None)
            margin_usdc = usdc_row["raw_balance"] if usdc_row else 0
            if amount > margin_usdc:
                return {**base, "allowed": False, "reason": f"Not enough USDC in the margin account ({margin_usdc / 1e6:g} available)"}
            deposit = amount
        trader_collateral = in_phoenix + deposit
        if trader_collateral <= 0:
            return {**base, "allowed": False, "reason": "Add USDC to trade on Phoenix"}

        is_long = side == "long"
        slip_bps = slippage_bps if slippage_bps is not None else default_slippage_bps(m.meta.key)
        ref = ref_price(m, live_mark)
        if not ref > 0:
            return {**base, "allowed": False, "reason": f"No price for {m.meta.name}"}
        limit = ref * (1 + slip_bps / 10_000) if is_long else ref * (1 - slip_bps / 10_000)
        size_usd = (amount if amount > 0 else trader_collateral) / 1e6 * leverage
        current = next((p.base_lots for p in (venue.trader.positions if venue.trader else ()) if p.asset_id == m.entry.asset_id), 0)
        others = sum(
            abs(p.virtual_quote_lots) / 1e6
            for p in (venue.trader.positions if venue.trader else ())
            if p.asset_id != m.entry.asset_id and p.base_lots != 0
        )
        cap_usd = trader_collateral / 1e6 * max_lev - others
        lots = int(size_usd / limit * 10**m.market.lot_decimals)
        if lots <= 0:
            one_lot = 10 ** -m.market.lot_decimals
            return {**base, "allowed": False, "reason": f"Size too small for {m.meta.name} (one lot is {one_lot:g} {m.meta.key})"}

        def after(lot_count: int) -> float:
            return abs(lots_to_tokens(current + (lot_count if is_long else -lot_count), m.market.lot_decimals)) * limit

        while lots > 0 and after(lots) > cap_usd:
            lots = lots * 995 // 1000
        if lots <= 0:
            return {**base, "allowed": False, "reason": f"Over {m.meta.name}'s {max_lev:g}x cap: add collateral or reduce size"}

        size_tokens = lots_to_tokens(lots, m.market.lot_decimals)
        order_usd = size_tokens * ref
        fee_usd = order_usd * (m.orderbook.taker_fee_micro if m.orderbook else 0) / 1e6
        # The USDC moved into Phoenix stays collateral (as the venue's value); only the open fee leaves the account.
        holdings_after = snap.health.total_collateral_value / _NANO - fee_usd
        debt = snap.health.total_debt_value / _NANO
        hf_after = holdings_after / debt if debt > 0 else None
        # The position after the order, for its liquidation price (same formula as `positions`).
        net_lots = current + (lots if is_long else -lots)
        net_tokens = abs(lots_to_tokens(net_lots, m.market.lot_decimals))
        net_long = net_lots > 0
        venue_after = venue.value_nano / _NANO + deposit / 1e6 - fee_usd
        index = m.index_usd if m.index_usd is not None else ref
        liq = None
        if debt > 0 and net_tokens > 0:
            zero_equity = index - venue_after / net_tokens if net_long else index + venue_after / net_tokens
            room = (holdings_after - LIQUIDATION_HF * debt) / net_tokens
            candidate = index - room if net_long else index + room
            reachable = candidate > zero_equity if net_long else candidate < zero_equity
            liq = candidate if reachable and candidate > 0 else None
        trader = venue.trader
        needs_registration = trader is None or (trader.capabilities & FULL_TRADER_CAPABILITIES) != FULL_TRADER_CAPABILITIES
        return {
            **base,
            "allowed": True,
            "reason": None,
            "collateral_usdc": math_utils.ui_amount(amount, 6),
            "uses_existing_phoenix_balance": deposit == 0,
            "leverage": leverage,
            "size": _num(size_tokens, 6),
            "size_usd": _num(order_usd, 2),
            "reference_price": _num(ref, 6),
            "limit_price": _num(limit, 6),
            "slippage_bps": slip_bps,
            "reduced_to_fit_cap": lots < int(size_usd / limit * 10**m.market.lot_decimals),
            "open_fee_usd": _num(fee_usd, 4),
            "current_health_factor": snap.health.health_factor_str,
            "projected_health_factor": _num(hf_after, 4),
            "liquidation_price_after": _num(liq, 6),
            "needs_trader_registration": trader is None,
            "needs_phoenix_access": trader is not None and needs_registration,
            "notes": (["The first Phoenix trade needs one extra wallet signature to open the Phoenix trading account."] if trader is None else [])
            + ["Perps leverage is Phoenix's own: no Vanna loan is created. The order fills now or not at all (immediate-or-cancel)."],
            "_market": m,
            "_lots": lots,
            "_limit": limit,
            "_amount": deposit,
            "_is_long": is_long,
        }

    # ── writes ────────────────────────────────────────────────────────────────────────────────────────
    # Every write returns an unsigned envelope; the wallet signs. Perps leverage is Phoenix's own: nothing here borrows.

    async def _helper(self, path: str, body: dict) -> dict | None:
        """Fork-only app helpers (book refresh, onboarding stand-in). None off a fork or on any failure."""
        settings = self._client.settings
        if not settings.is_fork:
            return None
        import httpx

        from vanna_core.app_gate import app_request

        base = (settings.perps_helper_url or settings.price_keeper_url).rstrip("/")
        try:
            async with pooled_http(30.0) as http:
                resp = await app_request(http, settings, "POST", f"{base}{path}", json=body)
            return resp.json() if resp.status_code == 200 else None
        except Exception:  # noqa: BLE001 - a missing helper leaves the fork's own state; the simulation decides
            return None

    async def refresh_vault_book(self, market: str) -> None:
        """Fork: before a Basis vault call, re-copy the market's book and asset map from mainnet (the app's
        `phoenix-book` route with `vault: true`); Basis enforces its price band against the fork's mark."""
        await self._helper("/api/perps/phoenix-book", {"market": market_key(market), "vault": True})

    async def _refresh_book(self, market: str) -> float | None:
        """Fork: re-copy the market's book from mainnet before trading; returns mainnet Phoenix's mark for it."""
        res = await self._helper("/api/perps/phoenix-book", {"market": market_key(market)})
        mark = (res or {}).get("markUsd")
        return float(mark) if isinstance(mark, (int, float)) and mark > 0 else None

    async def _context(self, authority: Pubkey):
        """(snapshot, venue, registry, model) for building: the margin read fresh, its vault balances in the model."""
        from vanna_core.margin_execute import MarginModel

        snap, venue = await self._account(authority)
        if snap is None:
            raise ValueError("Create a margin account first")
        registry = snap.registry
        balances = {}
        for row in snap.collateral_rows:
            if row.get("is_venue"):
                continue
            balances[registry.by_symbol(row["symbol"], allow_unoffered=True).asset_index] = int(row["raw_balance"])
        return snap, venue, registry, MarginModel(snap.margin, balances)

    def _assets(self, registry, venue: PhoenixVenue):
        from vanna_core.phoenix import PHOENIX_EXCHANGE

        return (
            registry.by_mint(str(PHOENIX_EXCHANGE)),
            registry.by_symbol("USDC"),
            registry.by_mint(str(venue.exchange.canonical_mint)),
        )

    async def _envelope(self, authority: Pubkey, instructions: list, purpose: str, summary: dict):
        from vanna_core.margin_execute import EXEC_COMPUTE_UNITS

        return await self._client.build_write_envelope(
            instructions, authority, purpose=purpose, compute_unit_limit=EXEC_COMPUTE_UNITS,
            use_protocol_lookup_tables=True, summary=summary,
        )

    async def register(self, authority: Pubkey):
        """The first Phoenix trade's extra step: create the margin's Phoenix trader (the wallet pays its rent)."""
        from vanna_core.pdas import margin_pda
        from vanna_core.phoenix import register_trader_ix

        margin = margin_pda(authority, self._client.program_id)[0]
        venue = await self._venue(margin)
        if venue.trader is not None:
            raise ValueError("The Phoenix trading account already exists")
        return await self._client.build_write_envelope(
            [register_trader_ix(authority, margin)], authority, purpose="phoenix_register_trader",
            summary={"action": "perps_register", "trader": str(venue.trader_address)},
        )

    async def _ensure_access(self, authority: Pubkey, venue: PhoenixVenue) -> bool:
        """A registered trader without the deposit / withdraw / order capabilities gets them (fork: the app's onboarding
        stand-in). True when something changed."""
        trader = venue.trader
        if trader is None:
            raise ValueError("needs_trader_registration: the first Phoenix trade needs the Phoenix trading account created first")
        if (trader.capabilities & FULL_TRADER_CAPABILITIES) == FULL_TRADER_CAPABILITIES:
            return False
        await self._helper("/api/perps/phoenix-access", {"owner": str(authority)})
        return True

    async def open(
        self, authority: Pubkey, market: str, side: str, collateral_usdc: str, leverage: float,
        slippage_bps: int | None = None, funded: bool = False,
    ):
        """Open or add to a long / short (the app's `openPerp`): Ember wraps `collateral_usdc` of margin USDC into phUSD,
        `deposit_funds` moves it into the margin's Phoenix trader, then an immediate-or-cancel market order of
        collateral x leverage USD - all in one transaction. "0" uses the Phoenix balance already there.

        Each of those three calls re-checks the margin's health, pricing every asset it holds. On an account holding
        many assets the three no longer fit one transaction's compute budget; then only the funding (wrap + deposit) is
        returned, as a prerequisite (`summary.prerequisite_kind = "phoenix_fund"`). Once it lands, call open again with
        the same `collateral_usdc` and `funded=True`: the order is sized from that USDC, now in Phoenix, and nothing
        more moves."""
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices
        from vanna_core.margin_execute import build_chained_margin_execute
        from vanna_core.pdas import margin_pda
        from vanna_core.phoenix import (
            ASK, BID, DEPOSIT_FUNDS, EMBER_DEPOSIT, EMBER_PROGRAM_ID, PHOENIX_PROGRAM_ID, amount_data, deposit_accounts,
            ember_accounts, market_order_data, order_accounts, usd_to_ticks, validator_accounts,
        )

        await maybe_refresh_prices(self._client, self._client.settings)
        live_mark = await self._refresh_book(market)
        snap, venue, registry, model = await self._context(authority)
        if await self._ensure_access(authority, venue):
            snap, venue, registry, model = await self._context(authority)
            if venue.trader is None or (venue.trader.capabilities & FULL_TRADER_CAPABILITIES) != FULL_TRADER_CAPABILITIES:
                raise ValueError("The Phoenix trading account isn't enabled for deposits yet; try again in a moment")
        sized = self._size(snap, venue, market, side, collateral_usdc, leverage, slippage_bps, live_mark, funded=funded)
        if not sized.get("allowed"):
            raise ValueError(sized.get("reason") or "This order was refused")
        cap = self._client.settings.perps_max_action_usd
        if cap and float(sized["size_usd"]) > cap:
            raise ValueError(f"This order's size (${sized['size_usd']}) is over the ${cap:g} limit for one perps order")

        margin = margin_pda(authority, self._client.program_id)[0]
        venue_asset, usdc, phusd = self._assets(registry, venue)
        m, lots, amount = sized["_market"], sized["_lots"], sized["_amount"]
        # The validator prices the order's market and every open position (backend `dd46264`).
        open_ids = list(venue.open_asset_ids)
        fund: list = []
        if amount > 0:
            fund += await build_chained_margin_execute(
                self._client, authority, margin, model, registry,
                target_program=EMBER_PROGRAM_ID, data=amount_data(EMBER_DEPOSIT, amount),
                cpi_accounts=ember_accounts(venue.exchange, margin, Pubkey.from_string(usdc.mint)),
                spent=usdc, received=phusd, expected_spent=amount, expected_received=amount,
                venue=venue,  # a margin already holding Phoenix hands the oracle its accounts on this call too
            )
            fund += await build_chained_margin_execute(
                self._client, authority, margin, model, registry,
                target_program=PHOENIX_PROGRAM_ID, data=amount_data(DEPOSIT_FUNDS, amount),
                cpi_accounts=deposit_accounts(venue.exchange, margin),
                spent=phusd, expected_spent=amount,
                venue_asset=venue_asset, venue=venue, target_asset_ids=(), opens=True,
                validator_accounts=validator_accounts(venue.book, open_ids),
            )
        ixs = fund + await build_chained_margin_execute(
            self._client, authority, margin, model, registry,
            target_program=PHOENIX_PROGRAM_ID,
            data=market_order_data(BID if sized["_is_long"] else ASK, usd_to_ticks(m.market, sized["_limit"]), lots, False),
            cpi_accounts=order_accounts(venue.exchange, margin, m.market),
            venue_asset=venue_asset, venue=venue, target_asset_ids=(m.entry.asset_id,), opens=True,
            validator_accounts=validator_accounts(venue.book, [m.entry.asset_id, *open_ids]),
        )
        summary = {
            "action": "perps_open", "market": m.meta.key, "side": sized["side"], "leverage": leverage,
            "collateral_usdc": sized["collateral_usdc"], "size": sized["size"], "size_usd": sized["size_usd"],
            "limit_price": sized["limit_price"],
        }
        envelope = await self._envelope(authority, ixs, "margin_execute:phoenix_open", summary)
        if not fund or not _out_of_compute(envelope):
            return envelope
        # Too many health checks for one transaction: move the USDC into Phoenix first; the order follows.
        logger.info("perps open over the compute budget; returning the funding transaction first")
        return await self._envelope(authority, fund, "margin_execute:phoenix_fund", {
            **summary, "action": "perps_fund", "prerequisite": True, "prerequisite_kind": "phoenix_fund", "then": "perps_open",
            "note": f"Your account holds enough assets that the order doesn't fit in the same transaction: this one moves "
                    f"{sized['collateral_usdc']} USDC into Phoenix, then the order is built (open again with funded=true).",
        })

    async def close(self, authority: Pubkey, market: str, fraction: float | None = None, size_usd: str | None = None, slippage_bps: int | None = None):
        """Close all of a position, or part (`fraction` of it, or `size_usd` of it), with a reduce-only market order on
        the opposite side (the app's `closePerp`). Bringing the USDC back is a separate `withdraw`."""
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices
        from vanna_core.margin_execute import build_chained_margin_execute
        from vanna_core.pdas import margin_pda
        from vanna_core.phoenix import ASK, BID, PHOENIX_PROGRAM_ID, market_order_data, order_accounts, usd_to_ticks, validator_accounts

        await maybe_refresh_prices(self._client, self._client.settings)
        live_mark = await self._refresh_book(market)
        snap, venue, registry, model = await self._context(authority)
        key = market_key(market)
        m = next((x for x in venue.markets if x.meta.key == key), None)
        if m is None:
            raise ValueError(f"{key}-PERP is not a listed Phoenix market")
        base = next((p.base_lots for p in (venue.trader.positions if venue.trader else ()) if p.asset_id == m.entry.asset_id), 0)
        if base == 0:
            raise ValueError(f"No open {m.meta.name} position")
        is_long = base > 0
        whole = abs(base)
        ref = ref_price(m, live_mark)
        lots = whole
        if fraction is not None:
            if not 0 < fraction <= 1:
                raise ValueError("fraction must be above 0 and at most 1")
            lots = max(1, int(whole * fraction))
        elif size_usd is not None:
            lots = int(float(size_usd) / ref * 10**m.market.lot_decimals)
            if lots <= 0:
                raise ValueError(f"Size too small for {m.meta.name}")
        lots = min(lots, whole)
        slip = (slippage_bps if slippage_bps is not None else default_slippage_bps(m.meta.key)) / 10_000
        limit = ref * (1 - slip) if is_long else ref * (1 + slip)
        margin = margin_pda(authority, self._client.program_id)[0]
        venue_asset, _usdc, _phusd = self._assets(registry, venue)
        ixs = await build_chained_margin_execute(
            self._client, authority, margin, model, registry,
            target_program=PHOENIX_PROGRAM_ID,
            data=market_order_data(ASK if is_long else BID, usd_to_ticks(m.market, limit), lots, True),
            cpi_accounts=order_accounts(venue.exchange, margin, m.market),
            venue_asset=venue_asset, venue=venue, target_asset_ids=(m.entry.asset_id,), opens=False,
            validator_accounts=validator_accounts(venue.book, [m.entry.asset_id, *venue.open_asset_ids]),
        )
        return await self._envelope(authority, ixs, "margin_execute:phoenix_close", {
            "action": "perps_close", "market": m.meta.key, "side": "long" if is_long else "short",
            "size": f"{lots_to_tokens(lots, m.market.lot_decimals):g}", "closes_position": lots == whole,
            "limit_price": f"{limit:.6f}",
        })

    async def withdraw(self, authority: Pubkey, amount_usdc: str | None = None):
        """USDC back from Phoenix to the margin account (the app's `withdrawPerpCollateral`): `withdraw_funds`, then Ember
        turns the phUSD back into USDC. All of it by default; only what Phoenix pays now, never a queued withdrawal."""
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices
        from vanna_core.margin_execute import build_chained_margin_execute
        from vanna_core.pdas import margin_pda
        from vanna_core.phoenix import (
            EMBER_PROGRAM_ID, PHOENIX_PROGRAM_ID, WITHDRAW_FUNDS, amount_data, ember_accounts, ember_withdraw_data,
            validator_accounts, withdraw_accounts,
        )

        await maybe_refresh_prices(self._client, self._client.settings)
        snap, venue, registry, model = await self._context(authority)
        trader = venue.trader
        if trader is not None and (trader.hot or trader.withdraw_queued):
            raise ValueError("This Phoenix account has a queued withdrawal; try again shortly")
        collateral = trader.collateral if trader else 0
        if collateral <= 0:
            raise ValueError("No USDC in Phoenix to withdraw")
        requested = collateral if amount_usdc in (None, "", "all") else math_utils.base_units(amount_usdc, 6)
        amount = min(requested, collateral)
        if amount > venue.withdrawable_now:
            raise ValueError(f"Phoenix can pay out {venue.withdrawable_now / 1e6:g} USDC right now: try a smaller amount or wait a moment")
        empties = amount == collateral and not venue.positions
        margin = margin_pda(authority, self._client.program_id)[0]
        venue_asset, usdc, phusd = self._assets(registry, venue)
        ixs = await build_chained_margin_execute(
            self._client, authority, margin, model, registry,
            target_program=PHOENIX_PROGRAM_ID, data=amount_data(WITHDRAW_FUNDS, amount),
            cpi_accounts=withdraw_accounts(venue.exchange, margin),
            received=phusd, expected_received=amount,
            venue_asset=venue_asset, venue=venue, target_asset_ids=(), opens=False, closes=empties,
            validator_accounts=validator_accounts(venue.book, venue.open_asset_ids),
        )
        ixs += await build_chained_margin_execute(
            self._client, authority, margin, model, registry,
            target_program=EMBER_PROGRAM_ID, data=ember_withdraw_data(None),
            cpi_accounts=ember_accounts(venue.exchange, margin, Pubkey.from_string(usdc.mint)),
            spent=phusd, received=usdc, expected_spent=amount, expected_received=amount,
            venue=venue,  # still holding Phoenix unless this withdrawal emptied it
        )
        return await self._envelope(authority, ixs, "margin_execute:phoenix_withdraw", {
            "action": "perps_withdraw", "amount_usdc": f"{amount / 1e6:g}", "venue_emptied": empties,
        })
