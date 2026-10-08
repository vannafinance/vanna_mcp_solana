"""Phoenix perps as a Vanna venue: accounts, decoders and the valuation the oracle agent uses.

A port of the app's `lib/solana/phoenix.ts` and `lib/solana/phoenix-state.ts` (vanna-credit-backend main `ea8a408`,
itself a port of vanna-credit-layer `programs/vanna_oracle/src/phoenix.rs`). Same offsets, same seeds, same maths.

A margin trades on Phoenix through `margin_execute`: Ember turns margin USDC into phUSD (Phoenix's canonical USDC),
`deposit_funds` moves it into the margin's Phoenix trader account, and immediate-or-cancel market orders open and close
positions. The trader is the PDA `["trader", margin, 0u16]` of the Phoenix program, with the margin as its authority.

The venue is one collateral holding of the margin, keyed by the Phoenix exchange (its AssetConfig is
`["asset", exchange]`, kind = Venue). The oracle values it as the trader's collateral plus every position's equity
(index price, funding owed, the taker fee to close), floored at zero. Once a margin holds the venue, every
health-checked instruction must hand the oracle the Phoenix accounts listed by `venue_oracle_keys`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from solders.pubkey import Pubkey

from vanna_core.client import SolanaClient
from vanna_core.layouts import OracleConfig
from vanna_core.pricing import AccountData, OracleSpec, PriceUnavailableError, get_price

PHOENIX_PROGRAM_ID = Pubkey.from_string("EtrnLzgbS7nMMy5fbD42kXiUzGg8XQzJ972Xtk1cjWih")
EMBER_PROGRAM_ID = Pubkey.from_string("EMBERpYNE6ehWmXymZZS2skiFmCa9V5dp14e1iduM5qy")
# Phoenix's global config: the venue key (its AssetConfig and the oracle's market book are keyed by it).
PHOENIX_EXCHANGE = Pubkey.from_string("2zskx2iyCvb6Stg7RBZkt1f6MrF4dpYtMG3yMvKwqtUZ")
PHOENIX_LOG_AUTHORITY = Pubkey.from_string("GdxfTLSsdSY37G6fZoYtdGDSfgFnbT2EmRpuePZxWShS")
# phUSD: Phoenix's canonical USDC (Ember-wrapped 1:1); a collateral-only Vanna asset priced as USDC.
PHUSD_MINT = Pubkey.from_string("PhUsd11YkbjSaWjFncfAAmatntsjx3MgDR9B6g1ks3A")

# A trader entry key at or above this is an extension (native SOL), not a perp position.
_EXTENSION_KEY = 0xFF000000
# Trader flags (u32 at 96): bit 0 = hot; bits 1-5 = capabilities; an onboarded trader has all five.
TRADER_FLAGS_OFFSET = 96
FULL_TRADER_CAPABILITIES = 0x3E
ORDERBOOK_HEADER_LEN = 104
_ASSET_MAP_HEADER = 48
_ASSET_MAP_ENTRY = 1584
_ORACLE_CONFIG_LEN = 184
_NANO = 1_000_000_000


@dataclass(frozen=True)
class PerpMarketMeta:
    """The Phoenix markets Vanna lists (the oracle's market book holds their leverage caps)."""

    key: str  # Phoenix's own symbol and the app's market key
    name: str


PERP_MARKETS: tuple[PerpMarketMeta, ...] = (
    PerpMarketMeta("SOL", "SOL-PERP"),
    PerpMarketMeta("ETH", "ETH-PERP"),
    PerpMarketMeta("BTC", "BTC-PERP"),
    PerpMarketMeta("TSLA", "TSLA-PERP"),
    PerpMarketMeta("META", "META-PERP"),
)


# -- addresses ------------------------------------------------------------------------------------


def trader_pda(margin: Pubkey) -> Pubkey:
    """The margin's Phoenix trader (cross subaccount 0)."""
    return Pubkey.find_program_address([b"trader", bytes(margin), bytes([0, 0])], PHOENIX_PROGRAM_ID)[0]


def market_book_pda(oracle_program: Pubkey) -> Pubkey:
    """Vanna's Phoenix market book, an oracle-agent account keyed by the venue."""
    return Pubkey.find_program_address([b"market_book", bytes(PHOENIX_EXCHANGE)], oracle_program)[0]


def spline_pda(orderbook: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"spline", bytes(orderbook)], PHOENIX_PROGRAM_ID)[0]


def _arena_keys(header: Pubkey, count: int, seed: bytes) -> list[Pubkey]:
    extra = [Pubkey.find_program_address([seed, bytes([i + 1])], PHOENIX_PROGRAM_ID)[0] for i in range(max(count - 1, 0))]
    return [header, *extra]


def _key(data: bytes, at: int) -> Pubkey:
    return Pubkey.from_bytes(data[at : at + 32])


def _u16(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 2], "little")


def _u32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 4], "little")


def _u64(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 8], "little")


def _i64(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 8], "little", signed=True)


def _i8(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 1], "little", signed=True)


# -- decoders -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PhoenixExchange:
    canonical_mint: Pubkey  # phUSD
    global_vault: Pubkey
    perp_asset_map: Pubkey
    trader_index: tuple[Pubkey, ...]  # header + extra arenas (all passed writable to deposit / withdraw / order)
    active_buffer: tuple[Pubkey, ...]
    withdraw_queue: Pubkey
    ember_state: Pubkey
    ember_vault: Pubkey


@dataclass(frozen=True)
class PhoenixMarket:
    """One market of Phoenix's perp asset map."""

    symbol: str
    asset_id: int
    orderbook: Pubkey
    spline: Pubkey
    tick_size: int  # quote atoms (micro-USD) per base lot per tick
    lot_decimals: int  # base lots per token = 10^lot_decimals (can be negative)
    mark_ticks: int
    cumulative_funding: int
    isolated_only: bool


def decode_perp_asset_map(data: bytes) -> list[PhoenixMarket]:
    """`PerpAssetMap`: 48-byte header, 1584 bytes per asset."""
    markets: list[PhoenixMarket] = []
    for i in range(_u32(data, 32)):
        base = _ASSET_MAP_HEADER + _ASSET_MAP_ENTRY * i
        meta = base + 16
        if len(data) < meta + 1568 or data[meta + 1450] != 0 or data[meta + 1567] != 0:
            continue
        orderbook = _key(data, meta + 904)
        markets.append(
            PhoenixMarket(
                symbol=data[base : base + 16].rstrip(b"\0").decode("utf-8", "replace"),
                asset_id=_u16(data, meta + 944) | (_u16(data, meta + 948) << 16),
                orderbook=orderbook,
                spline=spline_pda(orderbook),
                tick_size=_u64(data, meta + 936),
                lot_decimals=_i8(data, meta + 946),
                mark_ticks=_u64(data, meta + 24),
                cumulative_funding=_i64(data, meta + 1312),
                isolated_only=data[meta + 1208] != 0,
            )
        )
    return markets


@dataclass(frozen=True)
class PhoenixOrderbook:
    status: int
    lot_decimals: int
    asset_id: int
    tick_size: int
    taker_fee_micro: int  # millionths of the notional


def decode_orderbook(data: bytes) -> PhoenixOrderbook:
    return PhoenixOrderbook(
        status=data[8],
        lot_decimals=_i8(data, 9),
        asset_id=_u32(data, 32),
        tick_size=_u64(data, 56),
        taker_fee_micro=_u32(data, 96),
    )


@dataclass(frozen=True)
class PhoenixPosition:
    asset_id: int
    base_lots: int
    virtual_quote_lots: int
    funding_snapshot: int


@dataclass(frozen=True)
class PhoenixTrader:
    collateral: int  # phUSD atoms (6 decimals) of cross-margin collateral
    capabilities: int
    hot: bool
    withdraw_queued: bool
    positions: tuple[PhoenixPosition, ...]


def decode_trader(data: bytes) -> PhoenixTrader:
    positions: list[PhoenixPosition] = []
    for j in range(_u64(data, 224)):
        at = 240 + 40 * j
        if len(data) < at + 40:
            break
        entry = _u64(data, at)
        if entry >= _EXTENSION_KEY:
            continue
        positions.append(
            PhoenixPosition(
                asset_id=entry & 0xFFFFFFFF,
                base_lots=_i64(data, at + 8),
                virtual_quote_lots=_i64(data, at + 16),
                funding_snapshot=_i64(data, at + 24),
            )
        )
    flags = _u32(data, TRADER_FLAGS_OFFSET)
    return PhoenixTrader(
        collateral=_i64(data, 88),
        capabilities=flags & FULL_TRADER_CAPABILITIES,
        hot=bool(flags & 1),
        withdraw_queued=_u32(data, 108) != 0,
        positions=tuple(positions),
    )


def withdraw_available(data: bytes, slot: int) -> int:
    """phUSD Phoenix pays out now without queueing (`WithdrawBudget::available`); 0 while anything is queued."""
    max_budget, remaining, replenish, last_slot, queued = (_u64(data, 24 + 8 * i) for i in range(5))
    if queued > 0:
        return 0
    refill = (slot - last_slot if slot > last_slot else 0) * replenish
    return min(remaining + refill, max_budget)


@dataclass(frozen=True)
class MarketBookEntry:
    market: Pubkey  # the Phoenix orderbook
    asset_id: int
    max_leverage_bps: int
    trading_enabled: bool
    index_price: OracleSpec


@dataclass(frozen=True)
class MarketBook:
    address: Pubkey
    collateral_mint: Pubkey
    collateral_decimals: int
    collateral_price: OracleSpec
    markets: tuple[MarketBookEntry, ...]


def _oracle_spec(data: bytes, at: int) -> OracleSpec:
    from vanna_core.assets import oracle_spec_from_config

    return oracle_spec_from_config(OracleConfig.parse(data[at : at + _ORACLE_CONFIG_LEN]))


def decode_market_book(address: Pubkey, data: bytes) -> MarketBook:
    """`MarketBook` (zero copy): collateral price at 108, entries at 292 (228 bytes each), count at 106."""
    markets = []
    for i in range(data[106]):
        at = 292 + 228 * i
        markets.append(
            MarketBookEntry(
                market=_key(data, at),
                asset_id=_u32(data, at + 32),
                max_leverage_bps=_u32(data, at + 36),
                trading_enabled=data[at + 40] != 0,
                index_price=_oracle_spec(data, at + 44),
            )
        )
    return MarketBook(
        address=address,
        collateral_mint=_key(data, 72),
        collateral_decimals=data[104],
        collateral_price=_oracle_spec(data, 108),
        markets=tuple(markets),
    )


# -- prices and valuation -------------------------------------------------------------------------


def ticks_to_usd(market: PhoenixMarket, ticks: int) -> float:
    return ticks * market.tick_size * 10**market.lot_decimals / 1e6


def lots_to_tokens(lots: int, lot_decimals: int) -> float:
    return lots / 10**lot_decimals


@dataclass(frozen=True)
class PositionEquity:
    value_usd: float
    pnl_usd: float
    funding_usd: float
    fee_usd: float
    equity_usd: float


def position_equity_usd(
    p: PhoenixPosition, cumulative_funding: int, index_usd: float, lot_decimals: int, taker_fee_micro: int, usdc_usd: float
) -> PositionEquity:
    """A position's equity in USD as the oracle computes it (`position_equity`): base value at the index price +
    virtual quote + funding owed - the taker fee to close."""
    value_usd = lots_to_tokens(p.base_lots, lot_decimals) * index_usd
    quote_usd = p.virtual_quote_lots / 1e6 * usdc_usd
    owed = (cumulative_funding - p.funding_snapshot) * p.base_lots
    funding_usd = -owed / 1e6 * usdc_usd
    fee_usd = abs(value_usd) * taker_fee_micro / 1e6
    return PositionEquity(value_usd, value_usd + quote_usd, funding_usd, fee_usd, value_usd + quote_usd + funding_usd - fee_usd)


@dataclass(frozen=True)
class ListedMarket:
    """One Vanna-listed Phoenix market with what reads and instruction builders need."""

    meta: PerpMarketMeta
    entry: MarketBookEntry
    market: PhoenixMarket
    orderbook: PhoenixOrderbook | None
    index_usd: float | None  # the price the oracle values positions at (None when no source is usable)
    mark_usd: float  # Phoenix's own mark price (what orders fill near)
    index_fresh: bool = False  # the index passed its max-age check


@dataclass(frozen=True)
class PositionView:
    key: str
    name: str
    asset_id: int
    is_long: bool
    base_lots: int
    size_tokens: float
    size_usd: float
    entry_price: float
    index_price: float
    mark_price: float
    pnl_usd: float
    funding_usd: float
    close_fee_usd: float
    equity_usd: float


@dataclass
class PhoenixVenue:
    """Everything about a margin's Phoenix venue a health scan, a Phoenix call or a read needs."""

    book: MarketBook
    exchange: PhoenixExchange
    markets: list[ListedMarket]
    trader: PhoenixTrader | None
    usdc_usd: float
    open_asset_ids: list[int]  # positions with exposure: their markets join the oracle segment
    positions: list[PositionView]
    collateral_usd: float  # phUSD sitting in the trader account
    value_nano: int  # the venue as the oracle values it (>= 0)
    open: bool  # the venue still holds something, so it stays on the collateral list
    withdrawable_now: int  # phUSD atoms Phoenix would pay out now without queueing
    trader_address: Pubkey = field(default=Pubkey.default())


def venue_oracle_keys(margin: Pubkey, venue: PhoenixVenue, extra_asset_ids: list[int] | tuple[int, ...] = ()) -> list[str]:
    """The oracle segment's Phoenix part (`price_keys`): market book, exchange, perp asset map, the trader, the USDC
    price accounts, then per market `[orderbook, ...index price accounts]` for the markets the trader holds plus
    `extra_asset_ids` (a call's target market)."""
    keys = [str(venue.book.address), str(PHOENIX_EXCHANGE), str(venue.exchange.perp_asset_map), str(trader_pda(margin))]
    keys += venue.book.collateral_price.accounts()
    for asset_id in dict.fromkeys([*venue.open_asset_ids, *extra_asset_ids]):
        entry = next((m for m in venue.book.markets if m.asset_id == asset_id), None)
        if entry is None:
            raise PriceUnavailableError(f"Phoenix market {asset_id} is not in Vanna's market book")
        keys += [str(entry.market), *entry.index_price.accounts()]
    return keys


# -- loading --------------------------------------------------------------------------------------

# The market book (changes only when an admin lists a market), the exchange's accounts and the perp asset map's asset
# count change rarely: reuse them for five minutes, like the app.
_STATIC_TTL_SECS = 300.0
_statics: dict[str, tuple[float, MarketBook, PhoenixExchange, int]] = {}


async def _read_exchange(client: SolanaClient) -> PhoenixExchange:
    raw = await client.get_multiple_accounts_raw([PHOENIX_EXCHANGE])
    entry = raw.get(str(PHOENIX_EXCHANGE))
    if entry is None:
        raise PriceUnavailableError("The Phoenix exchange was not found on this network")
    d = entry[1]
    trader_header, buffer_header = _key(d, 392), _key(d, 424)
    heads = await client.get_multiple_accounts_sliced([trader_header, buffer_header], 0, 64)
    t, b = heads.get(str(trader_header)), heads.get(str(buffer_header))
    return PhoenixExchange(
        canonical_mint=_key(d, 296),
        global_vault=_key(d, 328),
        perp_asset_map=_key(d, 360),
        trader_index=tuple(_arena_keys(trader_header, _u16(t[1], 52) if t else 1, b"global_trader_index")),
        active_buffer=tuple(_arena_keys(buffer_header, _u16(b[1], 52) if b else 1, b"active_trader_buffer")),
        withdraw_queue=_key(d, 472),
        ember_state=Pubkey.find_program_address([bytes(PHOENIX_PROGRAM_ID), b"state"], EMBER_PROGRAM_ID)[0],
        ember_vault=Pubkey.find_program_address([bytes(PHOENIX_PROGRAM_ID), b"vault"], EMBER_PROGRAM_ID)[0],
    )


async def _phoenix_statics(client: SolanaClient) -> tuple[MarketBook, PhoenixExchange, int] | None:
    cache_key = f"{client.settings.solana_rpc_url}|{client.settings.vanna_oracle_program_id}"
    now = time.monotonic()
    cached = _statics.get(cache_key)
    if cached and now - cached[0] < _STATIC_TTL_SECS:
        return cached[1], cached[2], cached[3]
    address = market_book_pda(Pubkey.from_string(client.settings.vanna_oracle_program_id))
    raw = await client.get_multiple_accounts_raw([address])
    entry = raw.get(str(address))
    if entry is None:
        return None
    book = decode_market_book(address, entry[1])
    exchange = await _read_exchange(client)
    head = (await client.get_multiple_accounts_sliced([exchange.perp_asset_map], 0, _ASSET_MAP_HEADER)).get(
        str(exchange.perp_asset_map)
    )
    asset_count = _u32(head[1], 32) if head else 0
    _statics[cache_key] = (now, book, exchange, asset_count)
    return book, exchange, asset_count


def clear_phoenix_cache() -> None:
    _statics.clear()


def _price(spec: OracleSpec, accounts: dict[str, AccountData], now: int) -> tuple[float | None, bool]:
    """The app's `priceFromAccounts`: (USD price, fresh) when a source is usable, else (None, False)."""
    from vanna_core.pricing import FRESH

    try:
        p = get_price(spec, "", accounts, now)
        return float(p.price.to_decimal_str()), bool(p.status & FRESH)
    except (PriceUnavailableError, ValueError):
        return None, False


def _price_usd(spec: OracleSpec, accounts: dict[str, AccountData], now: int) -> float | None:
    return _price(spec, accounts, now)[0]


async def load_phoenix_venue(client: SolanaClient, margin: Pubkey, now: int) -> PhoenixVenue | None:
    """Reads the market book, the exchange, the perp asset map, the margin's trader, the listed orderbooks and every
    price account involved, and values the venue. None when Vanna has no Phoenix market book on this network."""
    fixed = await _phoenix_statics(client)
    if fixed is None:
        return None
    book, exchange, asset_count = fixed

    price_keys = list(
        dict.fromkeys([*book.collateral_price.accounts(), *(k for m in book.markets for k in m.index_price.accounts())])
    )
    trader_address = trader_pda(margin)
    small = [trader_address, exchange.withdraw_queue, *(Pubkey.from_string(k) for k in price_keys)]
    # The big Phoenix accounts are read in slices: the used part of the perp asset map, the orderbook headers.
    infos, headers, asset_map, slot = await asyncio.gather(
        client.get_multiple_accounts_raw(small),
        client.get_multiple_accounts_sliced([m.market for m in book.markets], 0, ORDERBOOK_HEADER_LEN),
        client.get_multiple_accounts_sliced(
            [exchange.perp_asset_map], 0, _ASSET_MAP_HEADER + _ASSET_MAP_ENTRY * asset_count
        ),
        client.get_slot(),
    )
    accounts = {k: AccountData(owner=v[0], data=v[1]) for k, v in infos.items() if v is not None}
    trader_raw = infos.get(str(trader_address))
    trader = decode_trader(trader_raw[1]) if trader_raw and trader_raw[1] else None
    queue_raw = infos.get(str(exchange.withdraw_queue))
    withdrawable_now = withdraw_available(queue_raw[1], slot) if queue_raw else 0
    map_raw = asset_map.get(str(exchange.perp_asset_map))
    phoenix_markets = decode_perp_asset_map(map_raw[1]) if map_raw else []

    usdc_usd = _price_usd(book.collateral_price, accounts, now) or 1.0
    order = [m.key for m in PERP_MARKETS]
    markets: list[ListedMarket] = []
    for entry in book.markets:
        market = next((m for m in phoenix_markets if m.asset_id == entry.asset_id), None)
        meta = next((m for m in PERP_MARKETS if market is not None and m.key == market.symbol), None)
        if market is None or meta is None:
            continue
        ob = headers.get(str(entry.market))
        index_usd, index_fresh = _price(entry.index_price, accounts, now)
        markets.append(
            ListedMarket(
                meta=meta,
                entry=entry,
                market=market,
                orderbook=decode_orderbook(ob[1]) if ob else None,
                index_usd=index_usd,
                mark_usd=ticks_to_usd(market, market.mark_ticks),
                index_fresh=index_fresh,
            )
        )
    markets.sort(key=lambda m: order.index(m.meta.key))

    collateral_usd = trader.collateral / 1e6 * usdc_usd if trader else 0.0
    equity = collateral_usd
    positions: list[PositionView] = []
    open_asset_ids: list[int] = []
    for p in trader.positions if trader else ():
        if p.base_lots == 0 and p.virtual_quote_lots == 0:
            continue
        listed = next((m for m in markets if m.entry.asset_id == p.asset_id), None)
        if listed is None:
            continue
        open_asset_ids.append(p.asset_id)
        index_usd = listed.index_usd if listed.index_usd is not None else listed.mark_usd
        fee = listed.orderbook.taker_fee_micro if listed.orderbook else 0
        e = position_equity_usd(p, listed.market.cumulative_funding, index_usd, listed.market.lot_decimals, fee, usdc_usd)
        equity += e.equity_usd
        if p.base_lots == 0:
            continue
        size_tokens = abs(lots_to_tokens(p.base_lots, listed.market.lot_decimals))
        positions.append(
            PositionView(
                key=listed.meta.key,
                name=listed.meta.name,
                asset_id=p.asset_id,
                is_long=p.base_lots > 0,
                base_lots=p.base_lots,
                size_tokens=size_tokens,
                size_usd=size_tokens * index_usd,
                # virtual quote = -(entry notional) for a long, +(entry notional) for a short
                entry_price=abs(p.virtual_quote_lots / 1e6) / size_tokens if size_tokens > 0 else 0.0,
                index_price=index_usd,
                mark_price=listed.mark_usd,
                pnl_usd=e.pnl_usd,
                funding_usd=e.funding_usd,
                close_fee_usd=e.fee_usd,
                equity_usd=e.equity_usd,
            )
        )
    value_usd = max(0.0, equity)
    return PhoenixVenue(
        book=book,
        exchange=exchange,
        markets=markets,
        trader=trader,
        usdc_usd=usdc_usd,
        open_asset_ids=open_asset_ids,
        positions=positions,
        collateral_usd=collateral_usd,
        value_nano=int(value_usd * _NANO),
        open=bool(trader and (trader.collateral != 0 or positions)),
        withdrawable_now=withdrawable_now,
        trader_address=trader_address,
    )


# Titan's router program (swap provider beside Jupiter) and its "atlas" PDA; listed here so the protocol lookup table
# can carry them with the Phoenix accounts (`vanna_core/protocol_alt.py`).
TITAN_PROGRAM_ID = Pubkey.from_string("T1TANpTeScyeqVzzgNViGDNrkQ6qHz9KrSBS4aNXvGT")
TITAN_ATLAS = Pubkey.find_program_address([b"atlas"], TITAN_PROGRAM_ID)[0]


async def venue_lookup_addresses(client: SolanaClient) -> list[Pubkey]:
    """Protocol-wide Phoenix, Ember and Titan keys for the shared lookup table: programs, the exchange and its
    accounts, the market book, and each listed market's orderbook, spline and index price accounts. Per-user keys (the
    trader, the margin's vaults) are never in the table. Empty when this network has no Phoenix market book."""
    from vanna_core.pdas import integration_pda

    keys = [
        PHOENIX_PROGRAM_ID,
        EMBER_PROGRAM_ID,
        TITAN_PROGRAM_ID,
        TITAN_ATLAS,
        integration_pda(PHOENIX_PROGRAM_ID, client.program_id)[0],
        integration_pda(EMBER_PROGRAM_ID, client.program_id)[0],
        integration_pda(TITAN_PROGRAM_ID, client.program_id)[0],
    ]
    fixed = await _phoenix_statics(client)
    if fixed is None:
        return keys
    book, ex, asset_count = fixed
    keys += [
        PHOENIX_EXCHANGE,
        PHOENIX_LOG_AUTHORITY,
        PHUSD_MINT,
        book.address,
        ex.global_vault,
        ex.perp_asset_map,
        ex.withdraw_queue,
        ex.ember_state,
        ex.ember_vault,
        *ex.trader_index,
        *ex.active_buffer,
    ]
    keys += [Pubkey.from_string(k) for k in book.collateral_price.accounts()]
    map_raw = (
        await client.get_multiple_accounts_sliced([ex.perp_asset_map], 0, _ASSET_MAP_HEADER + _ASSET_MAP_ENTRY * asset_count)
    ).get(str(ex.perp_asset_map))
    markets = decode_perp_asset_map(map_raw[1]) if map_raw else []
    for entry in book.markets:
        market = next((m for m in markets if m.asset_id == entry.asset_id), None)
        keys.append(entry.market)
        if market is not None:
            keys.append(market.spline)
        keys += [Pubkey.from_string(k) for k in entry.index_price.accounts()]
    return keys



# -- instructions (the outside calls `margin_execute` wraps) ----------------------------------------

DEPOSIT_FUNDS = bytes([202, 39, 52, 211, 53, 20, 250, 88])
WITHDRAW_FUNDS = bytes([241, 36, 29, 111, 208, 31, 104, 217])
PLACE_MARKET_ORDER = bytes([90, 118, 192, 252, 192, 99, 39, 145])
EMBER_DEPOSIT = bytes([242, 35, 198, 137, 82, 225, 242, 182])
EMBER_WITHDRAW = bytes([183, 18, 70, 156, 148, 109, 161, 34])
REGISTER_TRADER = bytes([75, 243, 224, 167, 1, 5, 51, 32])  # sha256("global:register_trader")[..8]
BID = 0
ASK = 1
_IMMEDIATE_OR_CANCEL = 2
_REDUCE_ONLY = 128


def usd_to_ticks(market: PhoenixMarket, usd: float) -> int:
    return max(1, round(usd * 1e6 / (market.tick_size * 10**market.lot_decimals)))


def _u64le(v: int) -> bytes:
    return int(v).to_bytes(8, "little")


def _option_u64(v: int | None) -> bytes:
    return b"\x00" if v is None else b"\x01" + _u64le(v)


def market_order_data(side: int, price_in_ticks: int, lots: int, reduce_only: bool, min_base_lots_to_fill: int = 0) -> bytes:
    """`place_market_order` data: an immediate-or-cancel order in Phoenix's Borsh layout (the app's `marketOrderData`)."""
    return b"".join([
        PLACE_MARKET_ORDER,
        bytes([_IMMEDIATE_OR_CANCEL, side]),
        _option_u64(price_in_ticks),
        _u64le(lots),
        _option_u64(None),
        _u64le(min_base_lots_to_fill),
        _u64le(0),
        b"\x00",
        _option_u64(None),
        bytes(16),
        _option_u64(None),
        bytes([_REDUCE_ONLY if reduce_only else 0, 0]),
    ])


def amount_data(discriminator: bytes, amount: int) -> bytes:
    return discriminator + _u64le(amount)


def ember_withdraw_data(amount: int | None) -> bytes:
    """Ember withdraw of the whole phUSD vault (None) or `amount`."""
    return EMBER_WITHDRAW + _option_u64(amount)


def _ro(key: Pubkey):
    from solders.instruction import AccountMeta

    return AccountMeta(key, is_signer=False, is_writable=False)


def _w(key: Pubkey):
    from solders.instruction import AccountMeta

    return AccountMeta(key, is_signer=False, is_writable=True)


def vault_of(margin: Pubkey, mint: Pubkey) -> Pubkey:
    """The margin's classic-SPL token account for `mint` (USDC, phUSD)."""
    from vanna_core.pdas import TOKEN_PROGRAM_ID, find_associated_token_address

    return find_associated_token_address(margin, mint, TOKEN_PROGRAM_ID)


def ember_accounts(ex: PhoenixExchange, margin: Pubkey, usdc_mint: Pubkey) -> list:
    """Ember (USDC <-> phUSD): margin, state, USDC mint, phUSD mint, USDC vault, phUSD vault, Ember vault, token program."""
    from vanna_core.pdas import TOKEN_PROGRAM_ID

    return [
        _ro(margin), _ro(ex.ember_state), _ro(usdc_mint), _w(ex.canonical_mint),
        _w(vault_of(margin, usdc_mint)), _w(vault_of(margin, ex.canonical_mint)), _w(ex.ember_vault), _ro(TOKEN_PROGRAM_ID),
    ]


def _head(margin: Pubkey) -> list:
    return [_ro(PHOENIX_PROGRAM_ID), _ro(PHOENIX_LOG_AUTHORITY), _w(PHOENIX_EXCHANGE), _ro(margin)]


def _arenas(ex: PhoenixExchange) -> list:
    return [_w(k) for k in (*ex.trader_index, *ex.active_buffer)]


def deposit_accounts(ex: PhoenixExchange, margin: Pubkey) -> list:
    from vanna_core.pdas import TOKEN_PROGRAM_ID

    return [*_head(margin), _w(vault_of(margin, ex.canonical_mint)), _w(trader_pda(margin)), _w(ex.global_vault), _ro(TOKEN_PROGRAM_ID), *_arenas(ex)]


def withdraw_accounts(ex: PhoenixExchange, margin: Pubkey) -> list:
    from vanna_core.pdas import TOKEN_PROGRAM_ID

    return [
        *_head(margin), _w(trader_pda(margin)), _w(ex.perp_asset_map), _w(ex.global_vault),
        _w(vault_of(margin, ex.canonical_mint)), _ro(TOKEN_PROGRAM_ID), *_arenas(ex), _w(ex.withdraw_queue),
    ]


def order_accounts(ex: PhoenixExchange, margin: Pubkey, market: PhoenixMarket) -> list:
    return [*_head(margin), _w(trader_pda(margin)), _w(ex.perp_asset_map), *_arenas(ex), _w(market.orderbook), _w(market.spline)]


def register_trader_ix(payer: Pubkey, margin: Pubkey):
    """`register_trader`: creates the margin's Phoenix trader (cross subaccount 0, 128 position slots). Permissionless:
    the wallet pays the rent and the margin is the trader's authority without signing, so it is a plain instruction."""
    from solders.instruction import AccountMeta, Instruction

    data = REGISTER_TRADER + (128).to_bytes(4, "little") + (0).to_bytes(4, "little") + (0).to_bytes(2, "little")
    system = Pubkey.from_string("11111111111111111111111111111111")
    return Instruction(
        PHOENIX_PROGRAM_ID,
        data,
        [
            _ro(PHOENIX_PROGRAM_ID), _ro(PHOENIX_LOG_AUTHORITY), _ro(PHOENIX_EXCHANGE),
            AccountMeta(payer, is_signer=True, is_writable=True), _ro(margin), _w(trader_pda(margin)), _ro(system),
        ],
    )


def validator_accounts(book: MarketBook) -> list[Pubkey]:
    """The validator segment's accounts for a Phoenix call (after the validator program): market book, exchange."""
    return [book.address, PHOENIX_EXCHANGE]
