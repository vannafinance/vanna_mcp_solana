"""Fetches the oracle accounts an asset set reads (one batched RPC call plus the chain
clock) and prices assets exactly the way the program will: `pricing.get_price` over
the fetched bytes. All pricing decisions live in `vanna_core.pricing`; this module is
only I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from solders.pubkey import Pubkey

from vanna_core.assets import DiscoveredAsset
from vanna_core.client import SolanaClient
from vanna_core.pricing import AccountData, OraclePrice, get_price


@dataclass
class OracleView:
    """Oracle account bytes + the chain clock, fixed at one moment — every price read from
    one view is consistent with the others."""

    accounts: dict[str, AccountData]
    now: int

    def price(self, asset: DiscoveredAsset) -> OraclePrice:
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
        keys: list[str] = []
        for asset in assets:
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
        return OracleView(accounts=accounts, now=now)

    async def price(self, asset: DiscoveredAsset) -> tuple[OraclePrice, OracleView]:
        # A receipt's price reads its own oracle accounts only (Scope/Pyth are the underlying's,
        # pinned identically in its config), so no extra assets are needed.
        view = await self.view([asset])
        return view.price(asset), view
