"""Fetches the oracle accounts an asset set reads (one batched RPC call plus the chain
clock) and prices assets exactly the way the program will: `pricing.get_price` over
the fetched bytes. All pricing decisions live in `vanna_core.pricing`; this module is
only I/O.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Iterable

from solders.pubkey import Pubkey

from vanna_core.assets import DiscoveredAsset
from vanna_core.client import SolanaClient
from vanna_core.pricing import FRESH, AccountData, OraclePrice, PriceUnavailableError, get_price

logger = logging.getLogger(__name__)

# The last good Basis share price per mint (process memory only). The share is priced by simulating the oracle, which
# can fail on a transient RPC error (a 429 from the fork's upstream); like the app (backend `039a46b`), the last good
# price is used for up to an hour, flagged not fresh, instead of the shares counting as $0 and the health factor
# jumping. Not fresh means a health-checked write still refuses it (`require(ALL_CHECKS)`), as the program would.
_LAST_BASIS_PRICE: dict[str, tuple[float, OraclePrice]] = {}
_LAST_BASIS_MAX_AGE_SECONDS = 3_600.0


def clear_last_basis_prices() -> None:
    _LAST_BASIS_PRICE.clear()


@dataclass
class OracleView:
    """Oracle account bytes + the chain clock, fixed at one moment — every price read from
    one view is consistent with the others."""

    accounts: dict[str, AccountData]
    now: int
    # Basis vault shares: the oracle agent's own price, read once per view (`vault_pricing.basis_price`); None = the
    # valuation was unavailable (the app leaves such collateral unvalued).
    vault_prices: dict[str, OraclePrice | None] = field(default_factory=dict)

    def price(self, asset: DiscoveredAsset) -> OraclePrice:
        if asset.oracle.uses_vault_receipt:
            from vanna_core.vault_pricing import is_basis, perena_price

            if is_basis(asset):
                price = self.vault_prices.get(asset.mint)
                if price is None:
                    raise PriceUnavailableError(f"price_unavailable: {asset.symbol} collateral valuation is unavailable")
                return price
            return perena_price(asset.oracle, asset.mint, self.accounts, self.now)
        return get_price(asset.oracle, asset.mint, self.accounts, self.now)


def describe_price(price: OraclePrice, now: int) -> dict:
    """Caller-facing summary of one price and the checks it passed."""
    from vanna_core.pricing import CONFIDENCE_OK, FRESH, TWAP_OK

    return {
        "usd": price.price.to_decimal_str(),
        "source": price.source,
        "age_seconds": max(0, now - price.timestamp),
        "fresh": bool(price.status & FRESH),
        "twap_ok": bool(price.status & TWAP_OK),
        "confidence_ok": bool(price.status & CONFIDENCE_OK),
    }


class OracleReader:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client

    async def view(self, assets: Iterable[DiscoveredAsset]) -> OracleView:
        assets_list = list(assets)
        keys: list[str] = []
        for asset in assets_list:
            for key in asset.oracle.accounts():
                if key not in keys:
                    keys.append(key)
        fetched = await self._client.get_multiple_accounts_raw([Pubkey.from_string(k) for k in keys]) if keys else {}
        accounts: dict[str, AccountData] = {}
        for key in keys:
            entry = fetched.get(key)
            if entry is not None:
                accounts[key] = AccountData(owner=entry[0], data=entry[1])
        now = await self._client.get_chain_clock_unix_timestamp()
        view = OracleView(accounts=accounts, now=now)
        from vanna_core.vault_pricing import basis_price, is_basis
        from vanna_core.vaults import vault_by_mint

        for asset in assets_list:
            if asset.oracle.uses_vault_receipt and is_basis(asset) and asset.mint not in view.vault_prices:
                try:
                    price = await basis_price(self._client, vault_by_mint(asset.mint))
                    view.vault_prices[asset.mint] = price
                    _LAST_BASIS_PRICE[asset.mint] = (time.monotonic(), price)
                except Exception as exc:  # noqa: BLE001 - see _LAST_BASIS_PRICE
                    last = _LAST_BASIS_PRICE.get(asset.mint)
                    if last is not None and time.monotonic() - last[0] <= _LAST_BASIS_MAX_AGE_SECONDS:
                        logger.warning("%s valuation unavailable (%s); using the last good price, flagged stale", asset.symbol, exc)
                        view.vault_prices[asset.mint] = replace(last[1], status=last[1].status & ~FRESH)
                    else:
                        logger.warning("%s valuation unavailable: %s", asset.symbol, exc)
                        view.vault_prices[asset.mint] = None
        return view

    async def price(self, asset: DiscoveredAsset) -> tuple[OraclePrice, OracleView]:
        # A receipt's price reads its own oracle accounts only (Scope/Pyth are the underlying's,
        # pinned identically in its config), so no extra assets are needed.
        view = await self.view([asset])
        return view.price(asset), view
