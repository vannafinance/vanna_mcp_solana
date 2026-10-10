"""Kamino Lend farm reads — markets (with live Kamino APY) and positions.
Backs `vanna_farm_overview` (TOOLS_CATALOGUE.md §3.9). Read-only; the write
half (`vanna_farm_kamino`) is `vanna_core/contracts/farm_kamino.py`.

Kamino Liquidity (multi-asset LP) is not supported and cannot be — that
program rejects the CPI (`CPINotAllowed`, verified in
`vanna-credit-backend/lib/solana/kamino.ts`). Only single-asset Kamino Lend
receipts exist here.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import httpx
from solders.pubkey import Pubkey

from vanna_core import math_utils
from vanna_core.assets import KAMINO_MAIN_MARKET, KLEND_PROGRAM_ID, AssetRegistry, DiscoveredAsset, find_receipt
from vanna_core.client import SolanaClient
from vanna_core.contracts.kamino import KaminoContract
from vanna_core.margin_state import load_margin_state
from vanna_core.pdas import find_associated_token_address, margin_pda

logger = logging.getLogger(__name__)

_KAMINO_METRICS_URL = f"https://api.kamino.finance/kamino-market/{KAMINO_MAIN_MARKET}/reserves/metrics"


async def _fetch_kamino_metrics() -> dict[str, dict]:
    """Live Kamino main-market metrics, keyed by Kamino reserve address.
    Never guessed — an unreachable API means every metric is null, not a
    fabricated number (matches `vanna-credit-backend`'s own `/api/farm/pools`
    contract)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(_KAMINO_METRICS_URL, params={"env": "mainnet-beta"})
            resp.raise_for_status()
            rows = resp.json()
        return {row["reserve"]: row for row in rows}
    except Exception as exc:  # noqa: BLE001 — external API, never fatal to a read
        logger.warning("Kamino metrics unavailable: %s", exc)
        return {}


def _underlying_symbol(registry: AssetRegistry, liquidity_mint: str) -> str | None:
    try:
        return registry.by_mint(liquidity_mint).symbol
    except ValueError:
        return None


class FarmContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client
        self._kamino = KaminoContract(client)

    async def list_markets(self) -> dict:
        registry = AssetRegistry(self._client)
        metrics_by_reserve, _ = await asyncio.gather(_fetch_kamino_metrics(), registry.load())

        async def one(discovered: DiscoveredAsset) -> dict:
            kamino_reserve = discovered.oracle.klend_reserve  # pinned in the receipt's on-chain OracleConfig
            rate = await self._kamino.read_reserve_rate(
                Pubkey.from_string(kamino_reserve), KLEND_PROGRAM_ID, Pubkey.from_string(discovered.mint)
            )
            m = metrics_by_reserve.get(kamino_reserve, {})
            # UI-meaningful "1 receipt token = X underlying" — NOT the raw
            # total_liquidity/collateral_supply ratio, which mixes the
            # receipt's 6 decimals against the underlying's own decimals and
            # is only valid when applied to raw amounts (as
            # `underlying_for_receipts` does), never displayed directly.
            one_receipt_raw = 10**discovered.decimals
            underlying_per_receipt_raw = rate.underlying_for_receipts(one_receipt_raw)
            exchange_rate = Decimal(underlying_per_receipt_raw) / (Decimal(10) ** rate.liquidity_decimals)
            return {
                "symbol": discovered.symbol,
                "underlying": _underlying_symbol(registry, rate.liquidity_mint),
                "kamino_reserve": kamino_reserve,
                "exchange_rate": str(exchange_rate),
                "collateral_enabled": discovered.collateral_enabled,
                # Kamino's own units: a fraction (0.067 = 6.7% APY), not a
                # pre-multiplied percentage — multiply by 100 for display.
                "supply_apy_fraction": m.get("supplyApy"),
                "borrow_apy_fraction": m.get("borrowApy"),
                "total_supply_usd": m.get("totalSupplyUsd"),
                "total_borrow_usd": m.get("totalBorrowUsd"),
                "metrics_available": bool(m),
            }

        receipts = [a for a in registry.all() if a.price_source == "KaminoReceipt"]
        markets = await asyncio.gather(*(one(a) for a in receipts))
        return {"markets": list(markets)}

    async def get_positions(self, authority: Pubkey) -> dict:
        margin_address = margin_pda(authority, self._client.program_id)[0]
        registry = AssetRegistry(self._client)
        await registry.load()
        margin = await load_margin_state(self._client, margin_address, registry)

        active = margin.held_collateral_indexes
        assets = await asyncio.gather(*(registry.by_index(i) for i in active))
        receipts = [a for a in assets if a.price_source == "KaminoReceipt"]

        async def one(asset: DiscoveredAsset) -> dict:
            token_program = Pubkey.from_string(asset.token_program)
            vault = find_associated_token_address(margin_address, Pubkey.from_string(asset.mint), token_program)
            try:
                data = await self._client.fetch_raw_account(vault)
                raw_receipts = int.from_bytes(data[64:72], "little")
            except Exception:  # noqa: BLE001
                raw_receipts = 0
            rate = await self._kamino.read_reserve_rate(
                Pubkey.from_string(asset.oracle.klend_reserve), KLEND_PROGRAM_ID, Pubkey.from_string(asset.mint)
            )
            underlying_raw = rate.underlying_for_receipts(raw_receipts)
            return {
                "symbol": asset.symbol,
                "receipt_balance": math_utils.ui_amount(raw_receipts, asset.decimals),
                "underlying_amount": math_utils.ui_amount(underlying_raw, rate.liquidity_decimals),
            }

        positions = await asyncio.gather(*(one(a) for a in receipts))
        return {"margin_account": str(margin_address), "positions": list(positions)}
