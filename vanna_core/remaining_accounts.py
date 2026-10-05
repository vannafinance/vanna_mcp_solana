"""The `remaining_accounts` tail every health-checked write needs - a port of the layout
`risk_engine.rs::{split_positions, collect_holdings}` and `interface.rs::AgentAccounts` read
(verified against `vanna-credit-layer` origin/main `465f21a`, and the app's `buildHealthRemainingAccounts`):

1. Position groups, one per active position of the margin account EXCEPT the named ones
   the instruction values itself, in the margin's stored slot order:
   collateral group = `[asset_config, margin_vault]`,
   debt group       = `[asset_config, reserve, debt_position]`.
   Any missing, substituted, reordered or extra account fails the transaction closed with
   `IncompletePositionAccounts`, so no position can be hidden.
2. (`margin_execute` only) one `[asset_config, margin_vault]` group per token received for the first time.
3. Then the ORACLE segment `[vanna_oracle, price_book, <price accounts>]`. The core program no longer prices
   anything: it asks the oracle agent (a CPI) for every holding's value, handing it exactly this segment. So the
   segment names the price accounts (Scope / Pyth / Pyth factor / a receipt's klend reserve) of EVERY active
   asset - the instruction's own and any excluded from the position groups included - each once. Unlike the old
   layout the oracle looks only inside its own segment, so an account that also appears among a call's accounts
   must still be repeated here.

A margin that holds a venue asset (GMTrade perps) has a collateral group `[asset_config, venue_account]` and the
oracle values it from its market book. This server does not read that book, so it cannot build or value such an
account and says so (`UnpricablePositionError`) instead of building something the program would reject.

Shared by `margin_trade.py` (borrow / withdraw), `farm_kamino.py` and `swap.py` (via `margin_execute`).
"""

from __future__ import annotations

from typing import Iterable

from solders.instruction import AccountMeta
from solders.pubkey import Pubkey

from vanna_core.assets import AssetRegistry, DiscoveredAsset
from vanna_core.client import SolanaClient
from vanna_core.layouts import EMPTY_ASSET_INDEX
from vanna_core.pdas import debt_position_pda, find_associated_token_address, price_book_pda
from vanna_core.types import UnpricablePositionError


def _ro(key: Pubkey) -> AccountMeta:
    return AccountMeta(key, is_signer=False, is_writable=False)


def refuse_venue(asset: DiscoveredAsset) -> None:
    if asset.is_venue:
        raise UnpricablePositionError(
            "This margin account holds a GMTrade perps position. The Vanna MCP cannot value or build transactions "
            "for an account with perps yet; manage it in the Vanna app."
        )


def oracle_account_metas(assets: Iterable[DiscoveredAsset]) -> list[AccountMeta]:
    """Every price account the given assets may read (Scope, Pyth, Pyth factor, klend reserve), each exactly
    once, read-only."""
    seen: list[str] = []
    for asset in assets:
        for key in asset.oracle.accounts():
            if key not in seen:
                seen.append(key)
    return [_ro(Pubkey.from_string(key)) for key in seen]


def oracle_program_of(client: SolanaClient, assets: Iterable[DiscoveredAsset]) -> Pubkey:
    """The oracle agent every involved token names (one today). Read from the AssetConfig, falling back to the setting."""
    programs = {a.oracle_program for a in assets if a.oracle_program and not a.is_venue}
    if len(programs) > 1:
        raise UnpricablePositionError("The margin's assets are priced by more than one oracle program; this server supports one.")
    return Pubkey.from_string(next(iter(programs), client.settings.vanna_oracle_program_id))


def oracle_segment(client: SolanaClient, assets: Iterable[DiscoveredAsset]) -> list[AccountMeta]:
    """`[vanna_oracle, price_book, ...price accounts]` for every involved asset."""
    involved = list(assets)
    for asset in involved:
        refuse_venue(asset)
        if not asset.in_price_book:
            raise UnpricablePositionError(f"{asset.symbol} has no price source in the oracle's price book, so it cannot be valued.")
    program = oracle_program_of(client, involved)
    return [_ro(program), _ro(price_book_pda(program)[0]), *oracle_account_metas(involved)]


async def position_groups(
    client: SolanaClient,
    margin_address: Pubkey,
    margin_decoded,
    registry: AssetRegistry,
    exclude_collateral_indexes: set[int],
    exclude_debt_index: int | None,
    *,
    writable: bool = False,
) -> list[AccountMeta]:
    """The position groups only (no oracle segment). `writable` marks each vault, reserve and
    debt position writable - liquidation settles them; every other caller reads them."""
    accounts: list[AccountMeta] = []
    w = writable

    for index in [i for i in margin_decoded.collateral_asset_indexes if i != EMPTY_ASSET_INDEX]:
        asset = await registry.by_index(index)
        refuse_venue(asset)
        if index in exclude_collateral_indexes:
            continue
        vault = find_associated_token_address(
            margin_address, Pubkey.from_string(asset.mint), Pubkey.from_string(asset.token_program)
        )
        accounts.append(_ro(Pubkey.from_string(asset.config_address)))
        accounts.append(AccountMeta(vault, is_signer=False, is_writable=w))

    for index in [i for i in margin_decoded.debt_asset_indexes if i != EMPTY_ASSET_INDEX]:
        if exclude_debt_index is not None and index == exclude_debt_index:
            continue
        asset = await registry.by_index(index)
        reserve_address = Pubkey.from_string(asset.reserve)
        debt_position, _ = debt_position_pda(margin_address, reserve_address, client.program_id)
        accounts.append(_ro(Pubkey.from_string(asset.config_address)))
        accounts.append(AccountMeta(reserve_address, is_signer=False, is_writable=w))
        accounts.append(AccountMeta(debt_position, is_signer=False, is_writable=w))

    return accounts


async def active_assets(margin_decoded, registry: AssetRegistry) -> list[DiscoveredAsset]:
    indexes = [
        i for i in (*margin_decoded.collateral_asset_indexes, *margin_decoded.debt_asset_indexes) if i != EMPTY_ASSET_INDEX
    ]
    return [await registry.by_index(i) for i in dict.fromkeys(indexes)]


async def build_health_accounts(
    client: SolanaClient,
    margin_address: Pubkey,
    margin_decoded,
    registry: AssetRegistry,
    exclude_collateral_indexes: set[int],
    exclude_debt_index: int | None,
    extra_assets: Iterable[DiscoveredAsset] = (),
    extra_groups: Iterable[AccountMeta] = (),
) -> list[AccountMeta]:
    """Position groups, then `extra_groups` (`margin_execute`'s new-asset groups), then the oracle segment of every
    active asset plus `extra_assets` (the assets the instruction names itself, which may not be active yet)."""
    groups = await position_groups(
        client, margin_address, margin_decoded, registry, exclude_collateral_indexes, exclude_debt_index
    )
    involved = [*await active_assets(margin_decoded, registry), *extra_assets]
    unique = list(dict.fromkeys(involved))
    return [*groups, *extra_groups, *oracle_segment(client, unique)]
