"""Price reads through the program's oracle facade (`vanna_core.pricing`), plus Kamino-receipt
valuation. Backs the `vanna_oracle` dispatcher (TOOLS_CATALOGUE.md 3.1). Read-only - posting a
fresh price on the fork (the `dev-fork` adapter) is `oracle_refresh.py`.

An asset's price comes from its own on-chain `OracleConfig`: the Kamino Scope chain while fresh,
else the Pyth fallback when newer, x the klend exchange rate for receipts. Each price is reported
with the quality flags the program checks (fresh / TWAP / confidence), so a caller sees exactly
what a borrow would accept.
"""

from __future__ import annotations

from decimal import Decimal

from vanna_core.assets import AssetRegistry, DiscoveredAsset
from vanna_core.client import SolanaClient
from vanna_core.oracle_reader import OracleReader, OracleView, describe_price
from vanna_core.pricing import PriceUnavailableError


def _underlying_symbol(registry: AssetRegistry, liquidity_mint: str) -> str | None:
    try:
        return registry.by_mint(liquidity_mint).symbol
    except ValueError:
        return None


class OracleContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client
        self._reader = OracleReader(client)

    def _describe(self, asset: DiscoveredAsset, view: OracleView, registry: AssetRegistry) -> dict:
        price = view.price(asset)
        info = describe_price(price, view.now)
        oracle = asset.oracle
        result = {
            "symbol": asset.symbol,
            "price_usd": info["usd"],
            "source": info["source"],
            "publish_time": price.timestamp,
            "age_seconds": info["age_seconds"],
            "fresh": info["fresh"],
            "stale": not info["fresh"],
            "twap_ok": info["twap_ok"],
            "confidence_ok": info["confidence_ok"],
            "confidence_too_wide": not info["confidence_ok"],
            "max_age_secs": oracle.max_age_secs,
            "oracle": {
                "scope": oracle.uses_scope,
                "pyth_fallback": oracle.uses_pyth,
                "pyth_factor": oracle.uses_pyth_factor,
                "max_twap_divergence_bps": oracle.max_twap_divergence_bps,
                "max_confidence_bps": oracle.max_confidence_bps,
            },
        }
        if price.receipt_rate is not None:
            rate = price.receipt_rate
            one_receipt_raw = 10**asset.decimals
            underlying_ui = Decimal(rate.underlying_for_receipts(one_receipt_raw)) / (Decimal(10) ** rate.liquidity_decimals)
            underlying_price = Decimal(info["usd"])
            result.update(
                {
                    "source": "kamino_receipt",
                    "underlying_symbol": _underlying_symbol(registry, rate.liquidity_mint),
                    "priced_via": info["source"],
                    "underlying_price_usd": info["usd"],
                    "exchange_rate": str(underlying_ui),
                    "price_usd": str(underlying_ui * underlying_price),
                }
            )
        return result

    async def get_price(self, symbol: str) -> dict:
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(symbol)
        view = await self._reader.view([asset])
        return self._describe(asset, view, registry)

    async def get_prices(self, symbols: list[str]) -> dict[str, dict]:
        registry = AssetRegistry(self._client)
        await registry.load()
        assets: dict[str, DiscoveredAsset | None] = {}
        for symbol in symbols:
            try:
                assets[symbol] = registry.by_symbol(symbol)
            except ValueError:
                assets[symbol] = None
        view = await self._reader.view([a for a in assets.values() if a is not None])
        results: dict[str, dict] = {}
        for symbol, asset in assets.items():
            if asset is None:
                results[symbol] = {"error": "invalid_input", "message": f"{symbol!r} is not a registered asset"}
                continue
            try:
                results[symbol] = self._describe(asset, view, registry)
            except PriceUnavailableError as exc:
                results[symbol] = {"error": "price_unavailable", "message": str(exc)}
        return results
