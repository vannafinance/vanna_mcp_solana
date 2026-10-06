"""The asset registry - display catalogue plus runtime AssetConfig discovery.

TOOLS_CATALOGUE.md 1.2/1.3: a small static table gives symbols/decimals/icons
for the product's known assets, but on-chain `AssetConfig` accounts are the only
authority for flags, `asset_index`, decimals, token program and kind. How a token is
PRICED (its `OracleConfig`: Scope chain, Pyth fallback, klend receipt source) lives in the
ORACLE AGENT's price book (`vanna_oracle`, keyed by mint) since the agent split; the registry
reads the book in the same call and joins each token with its entry, so `DiscoveredAsset.oracle`
means what it always did. A venue asset (GMTrade perps) has no price-book entry: the oracle
values it from its market book, which this server does not read (see `is_venue`).
The registry discovers EVERY AssetConfig the program owns
(`getProgramAccounts`), so assets added on-chain (JupUSD, the Token-2022 xStocks
NVDAx/TSLAx, ...) show up without a code change. A static catalogue must never
cause an active collateral/debt position to be omitted from a health check - an
unknown `asset_index` stops the calling tool with `unsupported_asset`.
"""

from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass, field

from solders.pubkey import Pubkey

from vanna_core.pricing import OracleSpec

logger = logging.getLogger(__name__)

TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

# EMPTY_INDEX sentinel used for unused collateral/debt asset-index slots.
EMPTY_ASSET_INDEX = 65_535


@dataclass(frozen=True)
class AssetInfo:
    """Display-catalogue entry for one plain (non-receipt) asset."""

    symbol: str
    mint: str
    decimals: int
    is_native: bool = False
    token_program: Pubkey = field(default=TOKEN_PROGRAM_ID)

    @property
    def mint_pubkey(self) -> Pubkey:
        return Pubkey.from_string(self.mint)


@dataclass(frozen=True)
class KaminoReceiptInfo:
    """Display-catalogue entry for one Kamino main-market collateral-only
    receipt asset. 6 decimals regardless of the underlying (verified,
    `lib/solana/kamino.ts` CTOKEN_DECIMALS)."""

    symbol: str
    underlying_symbol: str
    collateral_mint: str
    reserve: str
    liquidity_vault: str
    decimals: int = 6
    token_program: Pubkey = field(default=TOKEN_PROGRAM_ID)

    @property
    def collateral_mint_pubkey(self) -> Pubkey:
        return Pubkey.from_string(self.collateral_mint)

    @property
    def reserve_pubkey(self) -> Pubkey:
        return Pubkey.from_string(self.reserve)


# Kamino Lend main market (verified, `lib/solana/kamino.ts`).
KLEND_PROGRAM_ID = Pubkey.from_string("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD")
KAMINO_MAIN_MARKET = Pubkey.from_string("7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF")
KAMINO_MAIN_MARKET_AUTHORITY = Pubkey.from_string("9DrvZvyWh1HuAoZxvYWMvkf2XCzryCpGgHqrMjyDWpmo")
KAMINO_INSTRUCTIONS_SYSVAR = Pubkey.from_string("Sysvar1nstructions1111111111111111111111111")

# Jupiter v6 (verified, `lib/solana/config.ts`).
JUPITER_V6_PROGRAM_ID = Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")

# Pyth (verified, `lib/solana/config.ts`).
PYTH_RECEIVER_PROGRAM_ID = Pubkey.from_string("rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ")
PYTH_PUSH_ORACLE_PROGRAM_ID = Pubkey.from_string("pythWSnswVUd12oZpeFP8e9CVaEqJg25g1Vtc2biRsT")

WSOL_MINT = "So11111111111111111111111111111111111111112"

# The real, live registry — verified against `lib/solana/tokens.ts:32-88`.
ASSETS: tuple[AssetInfo, ...] = (
    AssetInfo(
        symbol="SOL",
        mint=WSOL_MINT,
        decimals=9,
        is_native=True,
    ),
    AssetInfo(
        symbol="USDC",
        mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        decimals=6,
    ),
    AssetInfo(
        symbol="USDT",
        mint="Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        decimals=6,
    ),
    AssetInfo(
        symbol="JitoSOL",
        mint="J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
        decimals=9,
    ),
    AssetInfo(
        symbol="JupSOL",
        mint="jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",
        decimals=9,
    ),
)

KAMINO_RECEIPTS: tuple[KaminoReceiptInfo, ...] = (
    KaminoReceiptInfo(
        symbol="kSOL",
        underlying_symbol="SOL",
        reserve="d4A2prbA2whesmvHaL88BH6Ewn5N4bTSU2Ze8P6Bc4Q",
        liquidity_vault="GafNuUXj9rxGLn4y79dPu6MHSuPWeJR6UtTWuexpGh3U",
        collateral_mint="2UywZrUdyqs5vDchy7fKQJKau2RVyuzBev2XKGPDSiX1",
    ),
    KaminoReceiptInfo(
        symbol="kUSDC",
        underlying_symbol="USDC",
        reserve="D6q6wuQSrifJKZYpR1M8R4YawnLDtDsMmWM1NbBmgJ59",
        liquidity_vault="Bgq7trRgVMeq33yt235zM2onQ4bRDBsY5EWiTetF4qw6",
        collateral_mint="B8V6WVjPxW1UGwVDfxH2d2r8SyT4cqn7dQRK6XneVa7D",
    ),
    KaminoReceiptInfo(
        symbol="kUSDT",
        underlying_symbol="USDT",
        reserve="H3t6qZ1JkguCNTi9uzVKqQ7dvt2cum4XiXWom6Gn5e5S",
        liquidity_vault="2Eff8Udy2G2gzNcf2619AnTx3xM4renEv4QrHKjS1o9N",
        collateral_mint="B8zf4kojJbwgCRKA7rLaLhRCZBGhgAJp8wPBVZZHMhSv",
    ),
    KaminoReceiptInfo(
        symbol="kJitoSOL",
        underlying_symbol="JitoSOL",
        reserve="EVbyPKrHG6WBfm4dLxLMJpUDY43cCAcHSpV3KYjKsktW",
        liquidity_vault="6sga1yRArgQRqa8Darhm54EBromEpV3z8iDAvMTVYXB3",
        collateral_mint="9ucQp7thL38MDDTSER5ou24QnVSTZFLevDsZC1cAFkKy",
    ),
    KaminoReceiptInfo(
        symbol="kJupSOL",
        underlying_symbol="JupSOL",
        reserve="DGQZWCY17gGtBUgdaFs1VreJWsodkjFxndPsskwFKGpp",
        liquidity_vault="FbbgvvnwfN25cyX52Zqut2ZTbtvCWZbhKS177gfdUd6K",
        collateral_mint="8euYpQt56z9hA2ZcFCQuWK6gAptrLcJuMZ5PtMpUwbuY",
    ),
)

# GMTrade (perps) venue: its AssetConfig is keyed by the venue's store, not a mint (`lib/solana/perps-accounts.ts`).
GMTRADE_PROGRAM_ID = Pubkey.from_string("Gmso1uvJnLbawvw7yezdfCDcPydwW2s2iqG3w6MDucLo")
GMTRADE_STORE = "CTDLvGGXnoxvqLyTpGzdGLg9pD6JexKxKXSV8tqqo8bN"

# Assets the program registers on-chain beyond the catalogue above - display names only (decimals, token program,
# oracle and lend/borrow capability always come from the AssetConfig and the chain). JupUSD and the Token-2022 xStocks
# are margin collateral and swap assets in the app (vanna-credit-backend `lib/solana/tokens.ts`); naming a mint here is
# what makes it a product asset (see `is_offered`). A venue (GMTrade) is named but never offered as a token.
EXTRA_ASSET_SYMBOLS: dict[str, str] = {
    GMTRADE_STORE: "GMTrade",
    "JuprjznTrTSp2UFa3ZBUFgwdAmtZCq4MQCwysN55USD": "JupUSD",
    "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh": "NVDAx",
    "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB": "TSLAx",
    "XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN": "GOOGLx",
    "XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp": "AAPLx",
}
VENUE_SYMBOLS = frozenset({"GMTrade"})
# Decimals of the named on-chain assets, for the planning maths that runs before the registry is read (the registry's
# AssetConfig stays the authority for every build). Same values as the app's registry.
EXTRA_ASSET_DECIMALS: dict[str, int] = {"JupUSD": 6, "NVDAx": 8, "TSLAx": 8, "GOOGLx": 8, "AAPLx": 8}

_BY_SYMBOL: dict[str, AssetInfo] = {a.symbol.upper(): a for a in ASSETS}
_RECEIPT_BY_SYMBOL: dict[str, KaminoReceiptInfo] = {r.symbol.upper(): r for r in KAMINO_RECEIPTS}
_RECEIPT_BY_UNDERLYING: dict[str, KaminoReceiptInfo] = {
    r.underlying_symbol.upper(): r for r in KAMINO_RECEIPTS
}
_RECEIPT_BY_MINT: dict[str, KaminoReceiptInfo] = {r.collateral_mint: r for r in KAMINO_RECEIPTS}


def find_asset(symbol: str) -> AssetInfo | None:
    """Case-insensitive lookup; "wSOL" is accepted as "SOL"."""
    upper = symbol.strip().upper()
    if upper == "WSOL":
        upper = "SOL"
    return _BY_SYMBOL.get(upper)


def get_asset(symbol: str) -> AssetInfo:
    asset = find_asset(symbol)
    if asset is None:
        raise ValueError(
            f"{symbol!r} is not a supported asset. Supported: "
            f"{sorted(a.symbol for a in ASSETS)}"
        )
    return asset


def find_receipt(symbol: str) -> KaminoReceiptInfo | None:
    return _RECEIPT_BY_SYMBOL.get(symbol.strip().upper())


def receipt_for_underlying(symbol: str) -> KaminoReceiptInfo | None:
    return _RECEIPT_BY_UNDERLYING.get(symbol.strip().upper())


def receipt_by_mint(mint: str) -> KaminoReceiptInfo | None:
    return _RECEIPT_BY_MINT.get(mint)


def all_symbols() -> list[str]:
    """Every plain-asset UI symbol, registry order."""
    return [a.symbol for a in ASSETS]


def known_mints() -> list[str]:
    """Every mint this module can name: catalogue assets, extra on-chain assets, receipts."""
    mints = [a.mint for a in ASSETS]
    mints += [m for m in EXTRA_ASSET_SYMBOLS if m not in mints]
    mints += [r.collateral_mint for r in KAMINO_RECEIPTS if r.collateral_mint not in mints]
    return mints


# ---------------------------------------------------------------------------
# Runtime discovery (§1.3): asset_index -> AssetConfig -> mint, validated.
# ---------------------------------------------------------------------------


ZERO_PUBKEY = "11111111111111111111111111111111"


@dataclass(frozen=True)
class DiscoveredAsset:
    """One validated on-chain AssetConfig, joined with its display symbol."""

    symbol: str
    asset_index: int
    config_address: str
    mint: str
    reserve: str  # all-zero pubkey for a receipt (collateral-only, no Vanna pool)
    decimals: int
    token_program: str
    collateral_enabled: bool
    borrow_enabled: bool
    oracle: OracleSpec  # the token's price-book entry (empty for a venue)
    max_collateral_per_margin: int
    liquidation_bonus_bps: int
    kind: int = 0  # 0 = Token, 1 = Venue (layouts.ASSET_KIND_*)
    oracle_program: str = ""  # the oracle agent program that values this asset
    settle_mint: str = ""  # a venue's settle token (USDC for GMTrade)
    in_price_book: bool = True  # False for a token the oracle has no source for (it cannot be priced)

    @property
    def is_venue(self) -> bool:
        return self.kind == 1

    @property
    def is_receipt(self) -> bool:
        return self.oracle.uses_klend

    @property
    def has_reserve(self) -> bool:
        return self.reserve != ZERO_PUBKEY

    @property
    def price_source(self) -> str:
        return "KaminoReceipt" if self.is_receipt else "Oracle"


# Discovered AssetConfig accounts are effectively static (an admin registers an asset once), and every
# tool call builds its own registry, so cache the raw accounts per (rpc, program) for a short TTL and
# remember when the RPC rejects getProgramAccounts (the hosted fork does) so it is not retried per call.
_REGISTRY_CACHE_TTL_SECS = 60.0
_GPA_RETRY_AFTER_SECS = 600.0
_registry_cache: dict[str, tuple[float, str, list, bytes | None]] = {}
_gpa_blocked_until: dict[str, float] = {}


def clear_registry_cache() -> None:
    _registry_cache.clear()
    _gpa_blocked_until.clear()


class AssetRegistry:
    """Discovers every `AssetConfig` the Vanna program owns and resolves symbols/indexes
    against them. Never silently drops an unknown active index - callers that hit a miss
    must raise `unsupported_asset` (TOOLS_CATALOGUE.md 1.3 step 4)."""

    def __init__(self, client: "SolanaClient") -> None:  # noqa: F821 - see client.py
        self._client = client
        self._by_index: dict[int, DiscoveredAsset] = {}
        self._by_symbol: dict[str, DiscoveredAsset] = {}
        self._by_mint: dict[str, DiscoveredAsset] = {}
        self._loaded = False
        self.discovery_mode: str | None = None

    async def load(self) -> None:
        from vanna_core.anchor_codec import account_discriminator
        from vanna_core.layouts import ASSET_CONFIG_ACCOUNT_LEN
        from vanna_core.layouts import AssetConfig as AssetConfigLayout

        discriminator = account_discriminator("AssetConfig")
        key = f"{self._client.settings.solana_rpc_url}|{self._client.program_id}"
        now = time.monotonic()
        cached = _registry_cache.get(key)
        if cached is not None and cached[0] > now:
            _, self.discovery_mode, found, book = cached
        else:
            found = None
            if _gpa_blocked_until.get(key, 0.0) <= now:
                try:
                    found = await self._client.get_program_accounts(
                        self._client.program_id,
                        data_size=ASSET_CONFIG_ACCOUNT_LEN,
                        discriminator=discriminator,
                    )
                    self.discovery_mode = "program_accounts"
                except Exception as exc:  # noqa: BLE001
                    logger.warning("getProgramAccounts unavailable (%s); falling back to known mints", exc)
                    _gpa_blocked_until[key] = now + _GPA_RETRY_AFTER_SECS
            if found is None:
                found = await self._load_known_mints(ASSET_CONFIG_ACCOUNT_LEN, discriminator)
                self.discovery_mode = "known_mints"
            book = await self._load_price_book()
            _registry_cache[key] = (now + _REGISTRY_CACHE_TTL_SECS, self.discovery_mode, found, book)
        sources = parse_price_book(book)
        discovered = [
            _decode_asset_config(address, AssetConfigLayout.parse(data[8:]), sources) for address, data in found
        ]
        # Stable, product-first ordering: catalogue assets, then receipts, then the rest by index.
        rank = {a.mint: i for i, a in enumerate(ASSETS)}
        rank.update({r.collateral_mint: 100 + i for i, r in enumerate(KAMINO_RECEIPTS)})
        discovered.sort(key=lambda d: (rank.get(d.mint, 1000), d.asset_index))
        for asset in discovered:
            self._by_index[asset.asset_index] = asset
            self._by_symbol[asset.symbol.upper()] = asset
            self._by_mint[asset.mint] = asset
        self._loaded = True

    async def _load_price_book(self) -> bytes | None:
        """The oracle agent's price book bytes (None when it has not been opened on this deployment)."""
        from vanna_core.pdas import price_book_pda

        oracle_program = Pubkey.from_string(self._client.settings.vanna_oracle_program_id)
        address = price_book_pda(oracle_program)[0]
        raw = await self._client.get_multiple_accounts_raw([address])
        entry = raw.get(str(address))
        if entry is None or entry[0] != str(oracle_program):
            return None
        return entry[1]

    async def _load_known_mints(self, data_len: int, discriminator: bytes) -> list[tuple[Pubkey, bytes]]:
        """Fallback discovery when the RPC rejects `getProgramAccounts`: derive the
        AssetConfig PDA of every mint this module knows and validate what exists. Assets
        registered on-chain outside this list are invisible in this mode, so an active
        position in one still surfaces as `unsupported_asset`, never a silent omission."""
        from vanna_core.pdas import asset_config_pda

        program_id = self._client.program_id
        addresses = [asset_config_pda(Pubkey.from_string(mint), program_id)[0] for mint in known_mints()]
        raw = await self._client.get_multiple_accounts_raw(addresses)
        found: list[tuple[Pubkey, bytes]] = []
        for address in addresses:
            entry = raw.get(str(address))
            if entry is None:
                continue
            owner, data = entry
            if owner != str(program_id) or len(data) != data_len or data[:8] != discriminator:
                logger.warning("skipping %s: not a valid AssetConfig (owner=%s, len=%d)", address, owner, len(data))
                continue
            found.append((address, data))
        return found

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("AssetRegistry.load() must run before use")

    def by_symbol(self, symbol: str, *, allow_unoffered: bool = False) -> DiscoveredAsset:
        """Resolve a symbol from user input. Assets registered on-chain but not in the product
        (PRODUCT_SYMBOLS) raise `asset_not_offered`; `allow_unoffered=True` is only for exit paths
        (withdraw / close) so a position in one can always be removed."""
        self._require_loaded()
        upper = symbol.strip().upper()
        if upper == "WSOL":
            upper = "SOL"
        if upper not in self._by_symbol:
            raise ValueError(f"{symbol!r} has no registered AssetConfig on this deployment")
        asset = self._by_symbol[upper]
        if not allow_unoffered and not is_offered(asset):
            from vanna_core.types import AssetNotOfferedError

            raise AssetNotOfferedError(
                f"{asset.symbol} is registered on-chain but not offered in the product. "
                f"Supported: {', '.join(sorted(PRODUCT_SYMBOLS))} and their Kamino receipts."
            )
        return asset

    def by_mint(self, mint: str) -> DiscoveredAsset:
        self._require_loaded()
        if mint not in self._by_mint:
            raise ValueError(f"mint {mint} has no registered AssetConfig on this deployment")
        return self._by_mint[mint]

    def all(self, *, include_unoffered: bool = False) -> list[DiscoveredAsset]:
        """Product assets (PRODUCT_SYMBOLS and their receipts). `include_unoffered=True` also returns
        assets registered on-chain outside the product - for decoding (history), never for offers."""
        self._require_loaded()
        assets = list(self._by_symbol.values())
        return assets if include_unoffered else [a for a in assets if is_offered(a)]

    async def by_index(self, asset_index: int) -> DiscoveredAsset:
        """Resolve an asset_index found in a MarginAccount slot."""
        self._require_loaded()
        if asset_index in self._by_index:
            return self._by_index[asset_index]
        raise ValueError(f"asset_index {asset_index} is not a registered asset (unsupported_asset)")


def _symbol_for_mint(mint: str) -> str:
    info = next((a for a in ASSETS if a.mint == mint), None)
    if info is not None:
        return info.symbol
    receipt = _RECEIPT_BY_MINT.get(mint)
    if receipt is not None:
        return receipt.symbol
    return EXTRA_ASSET_SYMBOLS.get(mint, mint[:6])


def parse_price_book(data: bytes | None) -> dict[str, OracleSpec]:
    """Decode the oracle agent's price book into {mint: OracleSpec}. Entries are 220 bytes after a 12-byte header;
    the count is a byte at offset 9 (see layouts.py)."""
    from vanna_core.layouts import (
        PRICE_BOOK_HEADER_LEN,
        PRICE_BOOK_MAX_SOURCES,
        PRICE_SOURCE_LEN,
        PriceSource,
        pubkey_bytes_to_base58,
    )

    sources: dict[str, OracleSpec] = {}
    if not data or len(data) < PRICE_BOOK_HEADER_LEN:
        return sources
    count = min(data[9], PRICE_BOOK_MAX_SOURCES)

    def key(raw: object) -> str:
        text = pubkey_bytes_to_base58(raw)
        return "" if text == ZERO_PUBKEY else text

    for i in range(count):
        start = PRICE_BOOK_HEADER_LEN + PRICE_SOURCE_LEN * i
        if len(data) < start + PRICE_SOURCE_LEN:
            break
        entry = PriceSource.parse(data[start : start + PRICE_SOURCE_LEN])
        o = entry.config
        sources[pubkey_bytes_to_base58(entry.mint)] = OracleSpec(
            scope_prices=key(o.scope_prices),
            scope_chain=tuple(int(x) for x in o.scope_chain),
            scope_twap_chain=tuple(int(x) for x in o.scope_twap_chain),
            pyth_price=key(o.pyth_price),
            pyth_factor=key(o.pyth_factor),
            klend_reserve=key(o.klend_reserve),
            klend_program=key(o.klend_program),
            max_age_secs=o.max_age_secs,
            max_twap_divergence_bps=o.max_twap_divergence_bps,
            max_confidence_bps=o.max_confidence_bps,
        )
    return sources


_EMPTY_ORACLE = OracleSpec(
    scope_prices="", scope_chain=(0xFFFF,) * 4, scope_twap_chain=(0xFFFF,) * 4, pyth_price="", pyth_factor="",
    klend_reserve="", klend_program="", max_age_secs=0, max_twap_divergence_bps=0, max_confidence_bps=0,
)


def _decode_asset_config(config_address: Pubkey, decoded: object, sources: dict[str, OracleSpec]) -> DiscoveredAsset:
    from vanna_core.layouts import pubkey_bytes_to_base58

    def key(raw: object) -> str:
        text = pubkey_bytes_to_base58(raw)
        return "" if text == ZERO_PUBKEY else text

    mint = pubkey_bytes_to_base58(decoded.mint)
    book_entry = sources.get(mint)
    return DiscoveredAsset(
        symbol=_symbol_for_mint(mint),
        asset_index=decoded.asset_index,
        config_address=str(config_address),
        mint=mint,
        reserve=pubkey_bytes_to_base58(decoded.reserve),
        decimals=decoded.decimals,
        token_program=pubkey_bytes_to_base58(decoded.token_program),
        collateral_enabled=decoded.collateral_enabled,
        borrow_enabled=decoded.borrow_enabled,
        oracle=book_entry if book_entry is not None else _EMPTY_ORACLE,
        max_collateral_per_margin=decoded.max_collateral_per_margin,
        liquidation_bonus_bps=decoded.liquidation_bonus_bps,
        kind=int(decoded.kind),
        oracle_program=pubkey_bytes_to_base58(decoded.oracle),
        settle_mint=key(decoded.settle_mint),
        in_price_book=book_entry is not None or int(decoded.kind) == 1,
    )


# Product scope: every token the app lists - the catalogue (SOL, USDC, USDT, JitoSOL, JupSOL) and the named on-chain
# assets above (JupUSD, NVDAx, TSLAx, GOOGLx, AAPLx) - plus the Kamino receipts farming produces. A registered asset with
# no name here is not offered: hidden from listings and refused for actions (`asset_not_offered`); positions in it still
# count in health reads (resolved by index), and withdraw/close stay allowed so nobody is stuck. A venue is never a token.
PRODUCT_SYMBOLS = frozenset([a.symbol for a in ASSETS] + [s for s in EXTRA_ASSET_SYMBOLS.values() if s not in VENUE_SYMBOLS])


def is_offered(asset: "DiscoveredAsset") -> bool:
    if asset.is_venue:
        return False
    if asset.symbol in PRODUCT_SYMBOLS:
        return True
    receipt = _RECEIPT_BY_MINT.get(asset.mint)
    return receipt is not None and receipt.underlying_symbol in PRODUCT_SYMBOLS


def symbol_decimals(symbol: str) -> int:
    """Token decimals for a product symbol (catalogue, then named on-chain assets); 9 when unknown."""
    asset = find_asset(symbol)
    if asset is not None:
        return asset.decimals
    receipt = find_receipt(symbol)
    if receipt is not None:
        return 6
    return EXTRA_ASSET_DECIMALS.get(symbol, 9)


def receipt_symbols() -> list[str]:
    return [r.symbol for r in KAMINO_RECEIPTS]


def collateral_only_symbols() -> list[str]:
    """Product assets that are margin collateral and swap assets but cannot be lent or borrowed."""
    return [s for s in sorted(PRODUCT_SYMBOLS) if s not in LEND_BORROW_SYMBOLS]


# Product policy (confirmed by the team), matching the app's `lendable` flag and the chain (only these three have a
# Vanna Earn reserve): only these assets are lendable (Earn) and borrowable. Every other product asset is margin
# collateral and swap only.
LEND_BORROW_SYMBOLS = frozenset({"SOL", "USDC", "USDT"})


def assert_lend_borrow_offered(symbol: str, action: str) -> None:
    from vanna_core.types import AssetNotOfferedError

    upper = symbol.strip().upper()
    upper = "SOL" if upper == "WSOL" else upper
    if upper not in LEND_BORROW_SYMBOLS:
        raise AssetNotOfferedError(
            f"{action} is not offered for {symbol}: only {', '.join(sorted(LEND_BORROW_SYMBOLS))} "
            f"can be lent or borrowed ({', '.join(collateral_only_symbols())} are collateral and swap only)."
        )


# The app's product rules for two actions, mirrored so every MCP client follows the website:
# - margin collateral deposits: every product token except these (`depositable: false` in the app's
#   `lib/solana/tokens.ts`; USDT stays lendable, borrowable and withdrawable, it is only not offered as a deposit);
# - Kamino farm: no new supply for these underlyings (`FARM_DELISTED_UNDERLYINGS` in the app's `lib/solana/farm.ts`);
#   an existing position can still be redeemed.
NOT_DEPOSITABLE_SYMBOLS = frozenset({"USDT"})
FARM_DELISTED_UNDERLYINGS = frozenset({"JitoSOL", "JupSOL"})


def assert_depositable(symbol: str) -> None:
    from vanna_core.types import AssetNotOfferedError

    asset = find_asset(symbol)
    name = asset.symbol if asset else symbol.strip()
    if name in NOT_DEPOSITABLE_SYMBOLS:
        offered = [s for s in sorted(PRODUCT_SYMBOLS) if s not in NOT_DEPOSITABLE_SYMBOLS]
        raise AssetNotOfferedError(
            f"{name} is not offered as a margin collateral deposit. It can still be lent, borrowed, repaid and withdrawn, "
            f"and held in the margin account by borrowing or swapping into it. Collateral deposits: {', '.join(offered)}."
        )


def assert_farm_listed(symbol: str) -> None:
    from vanna_core.types import AssetNotOfferedError

    asset = find_asset(symbol)
    name = asset.symbol if asset else symbol.strip()
    if name in FARM_DELISTED_UNDERLYINGS:
        raise AssetNotOfferedError(
            f"The Kamino farm for {name} is closed to new supply. An existing {name} farm position can still be redeemed."
        )
