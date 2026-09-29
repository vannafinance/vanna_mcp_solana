"""Oracle facade — a 1:1 Python port of `vanna-credit-layer/programs/vanna_lending/
src/oracle/{mod,price,scope,pyth}.rs` (read directly from source).

An asset is priced from its on-chain `OracleConfig`: a Kamino Scope chain first
(a fresh Scope price is used as is), the Pyth push feed (x an optional second
feed) as the fallback when Scope is stale or unused, then x a klend reserve's
cToken exchange rate for Kamino receipts. A price failing a quality check is
still returned, flagged, so each caller demands only the checks it needs — the
same contract as the program (`PriceStatus`).

Every function here is pure over already-fetched account bytes; fetching lives
in `OracleReader` (vanna_core/oracle_reader.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vanna_core import math_utils
from vanna_core.contracts.kamino import ReserveRate, parse_reserve_rate
from vanna_core.types import ContractCallError

SCOPE_CHAIN_UNUSED = 0xFFFF
SCOPE_MAX_ENTRIES = 512
SCOPE_PROGRAM_ID = "HFn8GnPADiny6XqUoWE8uRPPxb29ikn4yTuPa9MF2fWJ"
PYTH_RECEIVER_PROGRAM_ID = "rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ"

ORACLE_PRICES_DISCRIMINATOR = bytes([89, 128, 118, 221, 6, 72, 180, 146])
_PRICES_OFFSET = 40  # discriminator (8) | oracle_mappings (32) | prices
_DATED_PRICE_LEN = 56  # value u64 | exp u64 | last_updated_slot u64 | unix_timestamp u64 | generic_data [u8; 24]
ORACLE_PRICES_LEN = _PRICES_OFFSET + _DATED_PRICE_LEN * SCOPE_MAX_ENTRIES
_MAX_SCOPE_EXP = 38

PRICE_UPDATE_V2_DISCRIMINATOR = bytes([0x22, 0xF1, 0x23, 0x63, 0x9D, 0x7E, 0xF4, 0xCD])
# PriceUpdateV2: disc (8) | write_authority (32) | verification_level (1 for Full) | feed_id (32)
# | price i64 | conf u64 | exponent i32 | publish_time i64 | prev_publish_time i64 | ema_price i64 | ema_conf u64
_PYTH_VERIFICATION_OFFSET = 40
_PYTH_PRICE = 73
_PYTH_CONF = 81
_PYTH_EXPONENT = 89
_PYTH_PUBLISH_TIME = 93
_PYTH_EMA_PRICE = 109
_PYTH_MIN_LEN = 133
_PYTH_VERIFICATION_FULL = 1

I64_MAX = (1 << 63) - 1
U128_MAX = (1 << 128) - 1

FRESH = 1
TWAP_OK = 1 << 1
CONFIDENCE_OK = 1 << 2
ALL_CHECKS = FRESH | TWAP_OK | CONFIDENCE_OK
LIQUIDATION_CHECKS = FRESH


class PriceUnavailableError(ContractCallError):
    """No usable price for an asset (unconfigured, missing/never-written source, or a
    needed oracle account not provided). Fails closed — never a partial health picture."""


@dataclass(frozen=True)
class Price:
    """USD per whole token = value x 10^exponent (how Pyth and Scope store prices)."""

    value: int
    exponent: int

    @classmethod
    def new(cls, value: int, exponent: int) -> "Price":
        while value > I64_MAX:
            value //= 10
            exponent += 1
        if value <= 0:
            raise PriceUnavailableError("invalid_price: a zero price must never value anything")
        return cls(value, exponent)

    def checked_mul(self, other: "Price") -> "Price":
        return Price.new(self.value * other.value, self.exponent + other.exponent)

    def value_of(self, amount: int, decimals: int, round_up: bool) -> int:
        return math_utils.normalize_token_value(amount, self.value, self.exponent, decimals, round_up)

    def diverges_from(self, other: "Price", max_bps: int) -> bool:
        """|self - other| / self > max_bps. Prices too far apart to compare exactly
        (overflowing u128) count as diverging — same as the program."""
        exponent = min(self.exponent, other.exponent)

        def scaled(p: "Price") -> int | None:
            shift = p.exponent - exponent
            if shift > 38:
                return None
            v = p.value * 10**shift
            return v if v <= U128_MAX else None

        a, b = scaled(self), scaled(other)
        if a is None or b is None:
            return True
        diff, limit = abs(a - b) * 10_000, a * max_bps
        if diff > U128_MAX or limit > U128_MAX:
            return True
        return diff > limit

    def to_decimal_str(self) -> str:
        from decimal import Decimal

        text = format(Decimal(self.value).scaleb(self.exponent), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class OracleSpec:
    """The on-chain `OracleConfig` (accounts as base58 strings; '' / all-zero = unused)."""

    scope_prices: str
    scope_chain: tuple[int, ...]
    scope_twap_chain: tuple[int, ...]
    pyth_price: str
    pyth_factor: str
    klend_reserve: str
    klend_program: str
    max_age_secs: int
    max_twap_divergence_bps: int
    max_confidence_bps: int

    @property
    def uses_scope(self) -> bool:
        return bool(self.scope_prices)

    @property
    def uses_pyth(self) -> bool:
        return bool(self.pyth_price)

    @property
    def uses_pyth_factor(self) -> bool:
        return bool(self.pyth_factor)

    @property
    def uses_klend(self) -> bool:
        return bool(self.klend_reserve)

    @property
    def is_configured(self) -> bool:
        return self.uses_scope or self.uses_pyth

    @property
    def twap_check_enabled(self) -> bool:
        return self.max_twap_divergence_bps > 0

    def accounts(self) -> list[str]:
        """Every account `get_price` may read for this asset (unused ones dropped)."""
        return [a for a in (self.scope_prices, self.pyth_price, self.pyth_factor, self.klend_reserve) if a]


@dataclass(frozen=True)
class AccountData:
    owner: str
    data: bytes


@dataclass
class OraclePrice:
    price: Price
    receipt_rate: ReserveRate | None
    timestamp: int
    status: int
    source: str = ""  # "scope" | "pyth" | "pyth_x_pyth" (informational; not on-chain)
    twap_price: Price | None = field(default=None)

    def value_of(self, amount: int, decimals: int, round_up: bool) -> int:
        if self.receipt_rate is None:
            return self.price.value_of(amount, decimals, round_up)
        underlying = self.receipt_rate.underlying_for_receipts(amount)
        return self.price.value_of(underlying, self.receipt_rate.liquidity_decimals, round_up)

    def require(self, required: int) -> None:
        """Fail closed on the first missing check (mirrors `PriceStatus::require`)."""
        missing = required & ~self.status
        if missing & FRESH:
            raise PriceUnavailableError("stale_price: the price is older than the asset's max age")
        if missing & TWAP_OK:
            raise PriceUnavailableError("price_too_divergent_from_twap: TWAP check failed")
        if missing & CONFIDENCE_OK:
            raise PriceUnavailableError("confidence_too_wide: Pyth confidence exceeds the asset's limit")


def _u64_at(data: bytes, i: int) -> int:
    return int.from_bytes(data[i : i + 8], "little")


def _i64_at(data: bytes, i: int) -> int:
    return int.from_bytes(data[i : i + 8], "little", signed=True)


def scope_chain_entries(chain: tuple[int, ...]) -> list[int]:
    length = 0
    for entry in chain:
        if entry == SCOPE_CHAIN_UNUSED:
            break
        length += 1
    if length == 0 or any(e != SCOPE_CHAIN_UNUSED for e in chain[length:]):
        raise PriceUnavailableError("invalid_oracle_config: bad Scope chain")
    if any(e >= SCOPE_MAX_ENTRIES for e in chain[:length]):
        raise PriceUnavailableError("invalid_oracle_config: Scope entry out of range")
    return list(chain[:length])


def scope_is_unused(chain: tuple[int, ...]) -> bool:
    return all(e == SCOPE_CHAIN_UNUSED for e in chain)


def read_scope_chain(account: AccountData, chain: tuple[int, ...]) -> tuple[Price, int] | None:
    """The chain's price and its oldest entry's timestamp; None when an entry is
    zero (never written) — treated like a stale price, never a price of zero."""
    if account.owner != SCOPE_PROGRAM_ID:
        raise PriceUnavailableError("invalid_oracle_owner: Scope prices account has the wrong owner")
    data = account.data
    if len(data) < ORACLE_PRICES_LEN or data[:8] != ORACLE_PRICES_DISCRIMINATOR:
        raise PriceUnavailableError("invalid_price_feed: not a Scope OraclePrices account")
    product: Price | None = None
    timestamp = None
    for entry in scope_chain_entries(chain):
        at = _PRICES_OFFSET + _DATED_PRICE_LEN * entry
        value, exp, ts = _u64_at(data, at), _u64_at(data, at + 8), _u64_at(data, at + 24)
        if value == 0:
            return None
        if exp > _MAX_SCOPE_EXP:
            raise PriceUnavailableError("invalid_price: Scope exponent out of range")
        price = Price.new(value, -exp)
        product = price if product is None else product.checked_mul(price)
        timestamp = ts if timestamp is None else min(timestamp, ts)
    assert product is not None and timestamp is not None
    return product, timestamp


@dataclass(frozen=True)
class PythQuote:
    price: Price
    ema: Price
    publish_time: int
    confidence_ok: bool


def read_pyth(account: AccountData, max_confidence_bps: int) -> PythQuote:
    if account.owner != PYTH_RECEIVER_PROGRAM_ID:
        raise PriceUnavailableError("invalid_oracle_owner: Pyth price account has the wrong owner")
    data = account.data
    if len(data) < _PYTH_MIN_LEN or data[:8] != PRICE_UPDATE_V2_DISCRIMINATOR:
        raise PriceUnavailableError("invalid_price_feed: not a Pyth PriceUpdateV2 account")
    if data[_PYTH_VERIFICATION_OFFSET] != _PYTH_VERIFICATION_FULL:
        raise PriceUnavailableError("invalid_price_feed: Pyth update is not fully verified")
    price, conf = _i64_at(data, _PYTH_PRICE), _u64_at(data, _PYTH_CONF)
    exponent = int.from_bytes(data[_PYTH_EXPONENT : _PYTH_EXPONENT + 4], "little", signed=True)
    publish_time, ema = _i64_at(data, _PYTH_PUBLISH_TIME), _i64_at(data, _PYTH_EMA_PRICE)
    if price <= 0 or ema <= 0:
        raise PriceUnavailableError("invalid_price: non-positive Pyth price")
    return PythQuote(
        price=Price.new(price, exponent),
        ema=Price.new(ema, exponent),
        publish_time=publish_time,
        confidence_ok=conf * 10_000 <= price * max_confidence_bps,
    )


@dataclass
class _Quote:
    price: Price
    twap: tuple[Price, int] | None
    timestamp: int
    confidence_ok: bool
    source: str


def _find(accounts: dict[str, AccountData], key: str) -> AccountData:
    account = accounts.get(key)
    if account is None:
        raise PriceUnavailableError(f"invalid_price_source: oracle account {key} was not provided/found")
    return account


def get_price(
    oracle: OracleSpec,
    mint: str,
    accounts: dict[str, AccountData],
    now: int,
) -> OraclePrice:
    """The price of an asset with the checks it passed — `oracle::get_price`."""
    if not oracle.is_configured:
        raise PriceUnavailableError("price_unavailable: asset has no oracle configured")

    def is_fresh(ts: int) -> bool:
        return now - ts <= oracle.max_age_secs

    quote: _Quote | None = None
    if oracle.uses_scope:
        account = _find(accounts, oracle.scope_prices)
        chain = read_scope_chain(account, oracle.scope_chain)
        if chain is not None:
            twap = None
            if not scope_is_unused(oracle.scope_twap_chain):
                twap = read_scope_chain(account, oracle.scope_twap_chain)
            quote = _Quote(chain[0], twap, chain[1], True, "scope")

    if oracle.uses_pyth and not (quote is not None and is_fresh(quote.timestamp)):
        base = read_pyth(_find(accounts, oracle.pyth_price), oracle.max_confidence_bps)
        if not oracle.uses_pyth_factor:
            fallback = _Quote(base.price, (base.ema, base.publish_time), base.publish_time, base.confidence_ok, "pyth")
        else:
            factor = read_pyth(_find(accounts, oracle.pyth_factor), oracle.max_confidence_bps)
            ts = min(base.publish_time, factor.publish_time)
            fallback = _Quote(
                base.price.checked_mul(factor.price),
                (base.ema.checked_mul(factor.ema), ts),
                ts,
                base.confidence_ok and factor.confidence_ok,
                "pyth_x_pyth",
            )
        if quote is None or fallback.timestamp > quote.timestamp:
            quote = fallback

    if quote is None:
        raise PriceUnavailableError("price_unavailable: no usable price source")

    twap_ok = (not oracle.twap_check_enabled) or (
        quote.twap is not None
        and is_fresh(quote.twap[1])
        and not quote.price.diverges_from(quote.twap[0], oracle.max_twap_divergence_bps)
    )
    status = 0
    if is_fresh(quote.timestamp):
        status |= FRESH
    if twap_ok:
        status |= TWAP_OK
    if quote.confidence_ok:
        status |= CONFIDENCE_OK

    receipt_rate = None
    if oracle.uses_klend:
        from solders.pubkey import Pubkey

        reserve = _find(accounts, oracle.klend_reserve)
        if reserve.owner != oracle.klend_program:
            raise PriceUnavailableError("invalid_kamino_accounts: klend reserve has the wrong owner")
        receipt_rate = parse_reserve_rate(reserve.data, Pubkey.from_string(mint))

    return OraclePrice(
        price=quote.price,
        receipt_rate=receipt_rate,
        timestamp=quote.timestamp,
        status=status,
        source=quote.source,
        twap_price=quote.twap[0] if quote.twap else None,
    )
