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

A margin that holds the Phoenix venue (perps) has the collateral group `[venue asset_config, margin]` in its slot, and
the oracle segment ends with the venue's accounts (`phoenix.venue_oracle_keys`: market book, exchange, perp asset map,
the margin's trader, the USDC price accounts, each open market's orderbook and index price accounts), deduplicated
with the token price accounts before them - the app's `buildHealthRemainingAccounts`. A venue is never a token: it
cannot be deposited, withdrawn or swapped as one (`refuse_venue`).

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
    """A venue (Phoenix perps) is a holding, not a token: it cannot be deposited, withdrawn or swapped as one."""
    if asset.is_venue:
        raise UnpricablePositionError(
            f"{asset.symbol} is a perps venue, not a token: it can't be deposited, withdrawn or swapped as one. "
            "Use the perps actions to move funds in or out of it."
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


def oracle_segment(
    client: SolanaClient, assets: Iterable[DiscoveredAsset], venue_keys: Iterable[str] = ()
) -> list[AccountMeta]:
    """`[vanna_oracle, price_book, ...price accounts]` for every involved token, then the venue's accounts
    (`venue_keys`, when the margin holds or a call targets the Phoenix venue), each account once."""
    tokens = [a for a in assets if not a.is_venue]  # a venue has no price-book entry: `venue_keys` carry its accounts
    for asset in tokens:
        if not asset.in_price_book:
            raise UnpricablePositionError(f"{asset.symbol} has no price source in the oracle's price book, so it cannot be valued.")
    program = oracle_program_of(client, tokens)
    head = [str(program), str(price_book_pda(program)[0])]
    keys = list(dict.fromkeys([*head, *(m.pubkey.__str__() for m in oracle_account_metas(tokens)), *venue_keys]))
    return [_ro(Pubkey.from_string(k)) for k in keys]


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
        if index in exclude_collateral_indexes:
            continue
        if asset.is_venue:
            # The venue's group names the margin itself: the oracle values it from the margin's Phoenix trader.
            accounts.append(_ro(Pubkey.from_string(asset.config_address)))
            accounts.append(_ro(margin_address))
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


async def holds_venue(margin_decoded, registry: AssetRegistry) -> bool:
    """True when the Phoenix venue is on the margin's collateral list."""
    for index in margin_decoded.collateral_asset_indexes:
        if index != EMPTY_ASSET_INDEX and (await registry.by_index(index)).is_venue:
            return True
    return False


async def venue_keys_for(
    client: SolanaClient,
    margin_address: Pubkey,
    margin_decoded,
    registry: AssetRegistry,
    *,
    extra_asset_ids: Iterable[int] = (),
    always: bool = False,
    venue=None,
) -> list[str]:
    """The venue's oracle accounts when the margin holds the venue, or `always` for a call AT the venue (valued even
    before it is on the collateral list); `extra_asset_ids` adds the market a call targets. Empty otherwise."""
    if not always and not await holds_venue(margin_decoded, registry):
        return []
    from vanna_core.phoenix import load_phoenix_venue, venue_oracle_keys

    if venue is None:
        venue = await load_phoenix_venue(client, margin_address, await client.get_chain_clock_unix_timestamp())
    if venue is None:
        raise UnpricablePositionError("This margin account holds Phoenix perps, but Vanna's Phoenix market book was not found.")
    return venue_oracle_keys(margin_address, venue, list(extra_asset_ids))


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
    active asset plus `extra_assets` (the assets the instruction names itself, which may not be active yet), ending
    with the Phoenix venue's accounts when the margin holds perps."""
    groups = await position_groups(
        client, margin_address, margin_decoded, registry, exclude_collateral_indexes, exclude_debt_index
    )
    involved = [*await active_assets(margin_decoded, registry), *extra_assets]
    unique = list(dict.fromkeys(involved))
    venue = await venue_keys_for(client, margin_address, margin_decoded, registry)
    return [*groups, *extra_groups, *oracle_segment(client, unique, venue)]
