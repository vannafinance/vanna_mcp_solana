"""The `remaining_accounts` health tail every health-checked write needs - a port of the
layout `risk_engine.rs::{split_positions, scan_positions}` reads (verified against the
program source):

1. Position groups, one per active position of the margin account EXCEPT the named ones
   the instruction values itself, in the margin's stored slot order:
   collateral group = `[asset_config, margin_vault]`,
   debt group       = `[asset_config, reserve, debt_position]`.
   Any missing, substituted, reordered or extra account fails the transaction closed with
   `IncompletePositionAccounts`, so no position can be hidden.
2. Then the oracle accounts of every asset involved (the named ones included), each once,
   in any order. Every key is pinned in the asset's `OracleConfig`; a needed account that
   is missing is an error on-chain.

Shared by `margin_trade.py` (borrow / withdraw), `farm_kamino.py` and `swap.py` (via
`margin_execute`).
"""

from __future__ import annotations

from typing import Iterable

from solders.instruction import AccountMeta
from solders.pubkey import Pubkey

from vanna_core.assets import AssetRegistry, DiscoveredAsset
from vanna_core.client import SolanaClient
from vanna_core.layouts import EMPTY_ASSET_INDEX
from vanna_core.pdas import debt_position_pda, find_associated_token_address


def _ro(key: Pubkey) -> AccountMeta:
    return AccountMeta(key, is_signer=False, is_writable=False)


def oracle_account_metas(assets: Iterable[DiscoveredAsset], skip: Iterable[str] = ()) -> list[AccountMeta]:
    """Every oracle account the given assets may read (Scope, Pyth, Pyth factor, klend
    reserve), each exactly once, read-only. `skip` = keys the instruction already carries
    elsewhere (the program finds oracle accounts by key among all it received)."""
    skipped = set(skip)
    seen: list[str] = []
    for asset in assets:
        for key in asset.oracle.accounts():
            if key not in seen and key not in skipped:
                seen.append(key)
    return [_ro(Pubkey.from_string(key)) for key in seen]


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
    """The position groups only (no oracle accounts). `writable` marks each vault, reserve and
    debt position writable - liquidation settles them; every other caller reads them."""
    accounts: list[AccountMeta] = []
    w = writable

    for index in [i for i in margin_decoded.collateral_asset_indexes if i != EMPTY_ASSET_INDEX]:
        if index in exclude_collateral_indexes:
            continue
        asset = await registry.by_index(index)
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
    skip_oracle_keys: Iterable[str] = (),
) -> list[AccountMeta]:
    """Position groups followed by the oracle accounts of every active asset plus `extra_assets`
    (the assets the instruction names itself, which may not be active yet)."""
    groups = await position_groups(
        client, margin_address, margin_decoded, registry, exclude_collateral_indexes, exclude_debt_index
    )
    involved = [*await active_assets(margin_decoded, registry), *extra_assets]
    return [*groups, *oracle_account_metas(involved, skip_oracle_keys)]
