"""`dev-fork` price preparation - makes prices fresh on the Surfpool fork before every
health-checked write (DECISIONS.md D4).

Two sources, chosen by `PRICE_REFRESH_SOURCE`; only one may run against a fork, since both
write the same oracle accounts:

- **keeper** (default, owner decision): POST `{PRICE_KEEPER_URL}/api/prices/refresh`, the app's
  own price keeper (`vanna-credit-backend/app/api/prices/refresh/route.ts`). It rewrites each
  asset's PriceUpdateV2 with a live Jupiter USD price stamped with the fork clock, and heals
  cloned Kamino reserves whose `last_update.slot` is ahead of the fork. Called the way the app
  calls it (`lib/solana/price-refresh.ts ensureFreshPrices`): at most once per 60 s, concurrent
  callers sharing one request.
- **cheatcode**: `DevForkOracleAdapter` below, this server's own `surfnet_setAccount` writer.

The cheatcode adapter is ported from the program authority's own fork flow, `vanna-credit-layer/scripts/src/oracle.ts`
(`refreshOraclesOnFork`) and `devnet-pyth.ts` (`fabricatePrice`), verified against source:

- **Pyth feeds**: every Pyth account an asset's `OracleConfig` pins (`pyth_price`, `pyth_factor`)
  is fabricated as a fresh, fully-verified `PriceUpdateV2` (layout cloned from an existing feed
  account), priced from Jupiter USD prices with previous-price / reference fallbacks.
- **Scope**: Kamino's live `OraclePrices` account is copied from mainnet onto the fork with the
  entries the assets read (price and TWAP chains) restamped to the fork's clock - a fork's clock
  need not match mainnet's. If mainnet is unreachable the fork's own copy is restamped instead
  (values may then lag mainnet; reported as `source: "fork_restamped"`).

`surfnet_setAccount` is a raw JSON-RPC cheatcode on the same fork RPC this client already uses.
Development-only - on a real cluster the keepers keep both oracles fresh; never presented as
production oracle publication. Every health-checked write calls `maybe_refresh_prices` first.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
from solders.pubkey import Pubkey

from vanna_core.assets import PYTH_PUSH_ORACLE_PROGRAM_ID, PYTH_RECEIVER_PROGRAM_ID, WSOL_MINT, AssetRegistry, DiscoveredAsset
from vanna_core.client import SolanaClient
from vanna_core.config import VannaSettings
from vanna_core.pdas import price_feed_account
from vanna_core.pricing import (
    _DATED_PRICE_LEN,
    _PRICES_OFFSET,
    ORACLE_PRICES_DISCRIMINATOR,
    ORACLE_PRICES_LEN,
    SCOPE_CHAIN_UNUSED,
    SCOPE_PROGRAM_ID,
)
from vanna_core.types import ContractCallError

logger = logging.getLogger(__name__)

SCOPE_PRICES = "3t4JZcueEzTbVP6kLxXrL3VpWx45jDer4eqysweBchNH"

# PriceUpdateV2 byte offsets (disc 8 | write_authority 32 | verification 1 | feed_id 32 | price ...).
_DISCRIMINATOR = bytes([0x22, 0xF1, 0x23, 0x63, 0x9D, 0x7E, 0xF4, 0xCD])
_VERIFICATION_OFFSET = 40
_FEED_ID_OFFSET = 41
_PRICE_OFFSET = 73
_CONF_OFFSET = 81
_EXPONENT_OFFSET = 89
_PUBLISH_TIME_OFFSET = 93
_PREV_PUBLISH_TIME_OFFSET = 101
_EMA_PRICE_OFFSET = 109
_EMA_CONF_OFFSET = 117
_MIN_PRICE_ACCOUNT_SIZE = 134
_PRICE_EXPONENT = -8

_JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v3"
_JUPUSD_MINT = "JuprjznTrTSp2UFa3ZBUFgwdAmtZCq4MQCwysN55USD"
_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
_JUPSOL_MINT = "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v"


@dataclass(frozen=True)
class _Feed:
    name: str
    feed_id: str  # hex
    reference_usd: float  # last resort only; reported as source "reference"

    @property
    def account(self) -> Pubkey:
        return price_feed_account(self.feed_id, PYTH_PUSH_ORACLE_PROGRAM_ID)[0]


# Pyth feed ids the protocol reads (`constants.rs::pyth_feeds`, `devnet-env.ts::PYTH_FEED_IDS`).
_FEEDS: tuple[_Feed, ...] = (
    _Feed("usdc", "eaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a", 1.0),
    _Feed("usdt", "2b89b9dc8fdf9f34709a5b106b472f0f39bb6ca9ce04b0fd7f2e971688e2e53b", 1.0),
    _Feed("wsol", "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d", 118.0),
    _Feed("jupsolRate", "f8d8d6b6c866c8b2624fb5b679ae846738725e5fc887fa8e927c8d8645018a2b", 1.21),  # JUPSOL/SOL redemption rate
    _Feed("jupusd", "8ed858a2214e892c9371694fb6c8a9037b6ed4052c4edf209f8cb988484e81d9", 1.0),
)
_FEED_BY_ACCOUNT: dict[str, _Feed] = {str(f.account): f for f in _FEEDS}


async def _fetch_jupiter_usd_prices(mints: list[str]) -> dict[str, float]:
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            resp = await http.get(_JUPITER_PRICE_URL, params={"ids": ",".join(mints)})
        if resp.status_code != 200:
            return {}
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 - best-effort; falls back to previous/reference price
        logger.warning("Jupiter price/v3 unavailable: %s", exc)
        return {}
    return {mint: row["usdPrice"] for mint, row in body.items() if isinstance(row, dict) and row.get("usdPrice")}


def _build_price_account_data(template: bytes, feed_id_hex: str, value: float, now_unix: int) -> bytes:
    data = bytearray(template)
    if len(data) < _MIN_PRICE_ACCOUNT_SIZE:
        raise ContractCallError(f"template PriceUpdateV2 account too small ({len(data)} bytes)")
    data[0:8] = _DISCRIMINATOR
    data[_VERIFICATION_OFFSET] = 1  # fully verified, as the push oracle writes it
    data[_FEED_ID_OFFSET : _FEED_ID_OFFSET + 32] = bytes.fromhex(feed_id_hex.removeprefix("0x"))
    scale = 10 ** (-_PRICE_EXPONENT)
    raw_price = round(value * scale)
    conf = max(1, round(value * 0.002 * scale))
    data[_PRICE_OFFSET : _PRICE_OFFSET + 8] = raw_price.to_bytes(8, "little", signed=True)
    data[_CONF_OFFSET : _CONF_OFFSET + 8] = conf.to_bytes(8, "little")
    data[_EXPONENT_OFFSET : _EXPONENT_OFFSET + 4] = _PRICE_EXPONENT.to_bytes(4, "little", signed=True)
    data[_PUBLISH_TIME_OFFSET : _PUBLISH_TIME_OFFSET + 8] = now_unix.to_bytes(8, "little", signed=True)
    data[_PREV_PUBLISH_TIME_OFFSET : _PREV_PUBLISH_TIME_OFFSET + 8] = (now_unix - 1).to_bytes(8, "little", signed=True)
    data[_EMA_PRICE_OFFSET : _EMA_PRICE_OFFSET + 8] = raw_price.to_bytes(8, "little", signed=True)
    data[_EMA_CONF_OFFSET : _EMA_CONF_OFFSET + 8] = conf.to_bytes(8, "little")
    return bytes(data)


def _decode_existing_value(data: bytes) -> float | None:
    if len(data) < _MIN_PRICE_ACCOUNT_SIZE or bytes(data[:8]) != _DISCRIMINATOR:
        return None
    price = int.from_bytes(data[_PRICE_OFFSET : _PRICE_OFFSET + 8], "little", signed=True)
    exponent = int.from_bytes(data[_EXPONENT_OFFSET : _EXPONENT_OFFSET + 4], "little", signed=True)
    value = price * (10**exponent)
    return value if value > 0 else None


def restamp_scope_entries(data: bytes, entries: set[int], now_unix: int) -> bytes:
    """Rewrite the unix_timestamp of the given Scope entries (offset +24 in each DatedPrice)."""
    if len(data) < ORACLE_PRICES_LEN or bytes(data[:8]) != ORACLE_PRICES_DISCRIMINATOR:
        raise ContractCallError("not a Scope OraclePrices account")
    out = bytearray(data)
    for entry in entries:
        at = _PRICES_OFFSET + _DATED_PRICE_LEN * entry + 24
        out[at : at + 8] = now_unix.to_bytes(8, "little")
    return bytes(out)


def scope_entries_used(assets: list[DiscoveredAsset]) -> set[int]:
    entries: set[int] = set()
    for asset in assets:
        if asset.oracle.uses_scope:
            entries.update(e for e in (*asset.oracle.scope_chain, *asset.oracle.scope_twap_chain) if e != SCOPE_CHAIN_UNUSED)
    return entries


KEEPER_MIN_INTERVAL_SECS = 60.0

_keeper_lock = asyncio.Lock()
_keeper_last_refresh = 0.0
# Oracle accounts the app keeper reported writing in its last successful refresh (empty until one happens).
_keeper_covered_accounts: set[str] = set()
# The supplement (below) has its own throttle, independent of the keeper's.
_supplement_lock = asyncio.Lock()
_supplement_last_refresh = 0.0


async def refresh_via_keeper(settings: VannaSettings) -> dict | None:
    """POST the app's price keeper, at most once per KEEPER_MIN_INTERVAL_SECS per process.

    Returns the keeper's JSON body, or None when a refresh inside the window already covers
    this call. Raises ContractCallError when the keeper answers with an error.
    """
    global _keeper_last_refresh
    async with _keeper_lock:
        if time.monotonic() - _keeper_last_refresh < KEEPER_MIN_INTERVAL_SECS:
            return None
        headers = {"Content-Type": "application/json"}
        if settings.price_keeper_secret:
            headers["Authorization"] = f"Bearer {settings.price_keeper_secret}"
        url = f"{settings.price_keeper_url.rstrip('/')}/api/prices/refresh"
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.post(url, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not isinstance(body, dict) or not body.get("ok"):
            detail = body.get("error") if isinstance(body, dict) else None
            raise ContractCallError(f"price keeper refresh failed (HTTP {resp.status_code}): {detail or resp.text[:200]}")
        _keeper_last_refresh = time.monotonic()
        prices = body.get("prices")
        if isinstance(prices, list):
            _keeper_covered_accounts.clear()
            _keeper_covered_accounts.update(row["account"] for row in prices if isinstance(row, dict) and row.get("account"))
        return body


async def maybe_refresh_prices(client: SolanaClient, settings: VannaSettings) -> list[dict] | dict | None:
    """Called at the start of every health-checked write. Refreshes prices when
    `ORACLE_MODE=dev-fork` (the default); a no-op in `pyth-pull` mode. Never raises: a failed
    refresh just means the on-chain check may fail closed on a stale price, which is the correct
    fallback, not a reason to break the caller."""
    from vanna_core.config import OracleMode, PriceRefreshSource

    if settings.oracle_mode != OracleMode.DEV_FORK:
        return None
    try:
        if settings.price_refresh_source == PriceRefreshSource.KEEPER:
            try:
                return await refresh_via_keeper(settings)
            finally:
                await _supplement_uncovered(client, settings)
        return await DevForkOracleAdapter(client, settings).refresh_all_prices()
    except Exception as exc:  # noqa: BLE001
        logger.warning("dev-fork price refresh failed (continuing - checks will fail closed if stale): %s", exc)
        return None


async def _supplement_uncovered(client: SolanaClient, settings: VannaSettings) -> list[dict]:
    """Refresh the Pyth feeds that only a cheatcode write can keep fresh, next to the keeper.

    The app keeper writes a fixed set of feeds (SOL, USDC, USDT, JupSOL rate). An asset priced
    by Pyth ALONE (no Scope chain - JupUSD today) whose feed the keeper does not write goes
    stale after its max age and would fail every borrow / in-debt withdrawal that touches it.
    This writes exactly those feeds, and never one the keeper reported writing, so the two
    writers cannot touch the same account. Throttled like the keeper; never raises.
    """
    global _supplement_last_refresh
    if client is None:
        return []
    async with _supplement_lock:
        if time.monotonic() - _supplement_last_refresh < KEEPER_MIN_INTERVAL_SECS:
            return []
        try:
            registry = AssetRegistry(client)
            await registry.load()
            needed = {
                asset.oracle.pyth_price
                for asset in registry.all()
                if asset.oracle.uses_pyth and not asset.oracle.uses_scope
            } - _keeper_covered_accounts
            if not needed:
                _supplement_last_refresh = time.monotonic()
                return []
            results = await DevForkOracleAdapter(client, settings)._refresh_pyth(registry.all(), only=needed)
            _supplement_last_refresh = time.monotonic()
            return results
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pyth-only supplement refresh failed (continuing): %s", exc)
            return []


class DevForkOracleAdapter:
    def __init__(self, client: SolanaClient, settings: VannaSettings) -> None:
        self._client = client
        self._settings = settings

    async def _call_cheatcode(self, method: str, params: list) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                self._settings.solana_rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers={"Content-Type": "application/json"},
            )
        body = resp.json()
        if "error" in body:
            raise ContractCallError(f"{method} failed: {body['error'].get('message', body['error'])}")
        return body.get("result")

    async def _set_account(self, address: str, lamports: int, data: bytes, owner: str) -> None:
        await self._call_cheatcode(
            "surfnet_setAccount",
            [address, {"lamports": lamports, "data": data.hex(), "owner": owner, "executable": False, "rentEpoch": 0}],
        )

    async def refresh_all_prices(self) -> list[dict]:
        registry = AssetRegistry(self._client)
        await registry.load()
        assets = registry.all()
        results = await self._refresh_pyth(assets)
        results.extend(await self._refresh_scope(assets))
        return results

    async def _refresh_pyth(self, assets: list[DiscoveredAsset], only: set[str] | None = None) -> list[dict]:
        pinned = {k for a in assets for k in (a.oracle.pyth_price, a.oracle.pyth_factor) if k}
        if only is not None:
            pinned &= only
        unknown = pinned - _FEED_BY_ACCOUNT.keys()
        if unknown:
            logger.warning("Pyth accounts pinned by assets but unknown to the dev-fork adapter: %s", sorted(unknown))
        feeds = [_FEED_BY_ACCOUNT[k] for k in pinned if k in _FEED_BY_ACCOUNT]
        if not feeds:
            return []

        sol_account = next(f for f in _FEEDS if f.name == "wsol").account
        addresses = [f.account for f in feeds]
        raw_accounts, sol_template, now_unix, jupiter = await asyncio.gather(
            asyncio.gather(*(self._client.fetch_account_with_lamports_or_none(a) for a in addresses)),
            self._client.fetch_account_with_lamports_or_none(sol_account),
            self._client.get_chain_clock_unix_timestamp(),
            _fetch_jupiter_usd_prices([_USDC_MINT, _USDT_MINT, WSOL_MINT, _JUPUSD_MINT, _JUPSOL_MINT]),
        )

        live_usd = {
            "usdc": jupiter.get(_USDC_MINT),
            "usdt": jupiter.get(_USDT_MINT),
            "wsol": jupiter.get(WSOL_MINT),
            "jupusd": jupiter.get(_JUPUSD_MINT),
            # JUPSOL/SOL redemption rate = JupSOL USD / SOL USD
            "jupsolRate": (jupiter[_JUPSOL_MINT] / jupiter[WSOL_MINT]) if _JUPSOL_MINT in jupiter and WSOL_MINT in jupiter else None,
        }

        results = []
        for feed, existing in zip(feeds, raw_accounts, strict=True):
            template = existing if existing and bytes(existing[1][:8]) == _DISCRIMINATOR else sol_template
            if template is None or bytes(template[1][:8]) != _DISCRIMINATOR:
                raise ContractCallError(
                    "No existing PriceUpdateV2 template on the fork to clone the layout from - the fork "
                    "must have a cloned mainnet Pyth SOL feed account."
                )
            lamports, template_data = template

            value, source = live_usd.get(feed.name), "jupiter"
            if value is None and existing is not None:
                value, source = _decode_existing_value(existing[1]), "previous"
            if value is None:
                value, source = feed.reference_usd, "reference"

            data = _build_price_account_data(template_data, feed.feed_id, value, now_unix)
            await self._set_account(str(feed.account), lamports, data, str(PYTH_RECEIVER_PROGRAM_ID))
            results.append({"kind": "pyth", "feed": feed.name, "account": str(feed.account), "value": value, "source": source})
        return results

    async def _refresh_scope(self, assets: list[DiscoveredAsset]) -> list[dict]:
        entries = scope_entries_used(assets)
        if not entries:
            return []
        now_unix = await self._client.get_chain_clock_unix_timestamp()

        live = await self._fetch_mainnet_scope()
        source = "mainnet"
        if live is None:
            fork_copy = await self._client.fetch_account_with_lamports_or_none(Pubkey.from_string(SCOPE_PRICES))
            if fork_copy is None:
                raise ContractCallError("Scope prices unavailable from both mainnet and the fork")
            live, source = fork_copy, "fork_restamped"
        lamports, data = live
        await self._set_account(SCOPE_PRICES, lamports, restamp_scope_entries(data, entries, now_unix), SCOPE_PROGRAM_ID)
        return [{"kind": "scope", "account": SCOPE_PRICES, "entries_restamped": len(entries), "source": source}]

    async def _fetch_mainnet_scope(self) -> tuple[int, bytes] | None:
        import base64

        try:
            async with httpx.AsyncClient(timeout=20.0) as http:
                resp = await http.post(
                    self._settings.mainnet_rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [SCOPE_PRICES, {"encoding": "base64"}]},
                )
            value = resp.json()["result"]["value"]
            if value is None or value["owner"] != SCOPE_PROGRAM_ID:
                return None
            return value["lamports"], base64.b64decode(value["data"][0])
        except Exception as exc:  # noqa: BLE001 - mainnet RPC is public and rate limited; fall back
            logger.warning("Could not read Scope prices from mainnet (%s); restamping the fork's copy", exc)
            return None
