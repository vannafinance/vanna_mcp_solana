"""A margin account as the program sees it since the 2026-10-09 upgrade (the app's `fetchMarginAccount` and
`MarginIndexModel`, vanna-credit-backend main `2462473`).

Token collateral is protocol-wide: the program scans every margin over `ProtocolConfig.allowed_tokens` (in that order),
and the margin itself keeps only its venues (Phoenix, vault venues) in `venue_asset_indexes`. So:

- `collateral_asset_indexes` = allowed tokens + venues. This is the list the health `remaining_accounts` walk: one
  `[asset_config, margin_vault]` group per allowed token, zero balances included, then a group per venue.
- `vault` = every allowed token's margin-vault balance (one batched read).
- `held_collateral_indexes` = the tokens actually held (balance > 0) + the venues: what is valued, listed and priced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from solders.pubkey import Pubkey

from vanna_core.layouts import EMPTY_ASSET_INDEX
from vanna_core.layouts import MarginAccount as MarginAccountLayout
from vanna_core.layouts import ProtocolConfig as ProtocolConfigLayout
from vanna_core.pdas import find_associated_token_address, protocol_config_pda


def token_account_amount(data: bytes | None) -> int:
    """An SPL / Token-2022 account's amount (bytes 64..72); 0 when missing or too short."""
    if not data or len(data) < 72:
        return 0
    return int.from_bytes(data[64:72], "little")


@dataclass
class MarginState:
    address: Pubkey
    authority: object
    status: int
    venue_count: int
    debt_count: int
    venue_asset_indexes: list[int]
    debt_asset_indexes: list[int]
    event_sequence: int
    allowed_token_indexes: list[int]
    max_debts_per_margin: int
    max_venues_per_margin: int
    vault: dict[int, int] = field(default_factory=dict)

    @property
    def collateral_asset_indexes(self) -> list[int]:
        return [*self.allowed_token_indexes, *self.venue_asset_indexes]

    @property
    def held_collateral_indexes(self) -> list[int]:
        tokens = [i for i in self.allowed_token_indexes if self.vault.get(i, 0) > 0]
        return [*tokens, *(i for i in self.venue_asset_indexes if i != EMPTY_ASSET_INDEX)]

    @property
    def collateral_count(self) -> int:
        """Collateral positions actually held (tokens with a balance, and venues)."""
        return len(self.held_collateral_indexes)

    def is_allowed(self, asset_index: int) -> bool:
        return asset_index in self.allowed_token_indexes

    def with_vault(self, asset_index: int, delta: int) -> "MarginState":
        """A copy with one vault balance moved by `delta` (a deposit earlier in the same transaction)."""
        vault = dict(self.vault)
        vault[asset_index] = max(vault.get(asset_index, 0) + delta, 0)
        return MarginState(
            self.address, self.authority, self.status, self.venue_count, self.debt_count, list(self.venue_asset_indexes),
            list(self.debt_asset_indexes), self.event_sequence, list(self.allowed_token_indexes),
            self.max_debts_per_margin, self.max_venues_per_margin, vault,
        )


async def load_protocol_config(client):
    address, _ = protocol_config_pda(client.program_id)
    return await client.fetch_program_account(address, "ProtocolConfig", ProtocolConfigLayout)


def allowed_tokens_of(protocol) -> list[int]:
    return [int(i) for i in protocol.allowed_tokens if int(i) != EMPTY_ASSET_INDEX]


async def load_margin_state(client, margin_address: Pubkey, registry) -> MarginState:
    """The margin, the protocol's allowed tokens and every allowed token's vault balance. Raises
    `AccountNotFoundError` when the margin doesn't exist (as reading the account directly did)."""
    margin, protocol = await asyncio.gather(
        client.fetch_program_account(margin_address, "MarginAccount", MarginAccountLayout),
        load_protocol_config(client),
    )
    allowed = allowed_tokens_of(protocol)
    vaults: dict[int, Pubkey] = {}
    for index in allowed:
        try:
            asset = await registry.by_index(index)
        except ValueError:
            continue  # an allowed index this server can't resolve yet: health building names it and fails clearly
        vaults[index] = find_associated_token_address(
            margin_address, Pubkey.from_string(asset.mint), Pubkey.from_string(asset.token_program)
        )
    raw = await client.get_multiple_accounts_raw(list(vaults.values())) if vaults else {}
    balances = {index: token_account_amount((raw.get(str(addr)) or (None, None))[1]) for index, addr in vaults.items()}
    return MarginState(
        address=margin_address,
        authority=margin.authority,
        status=int(margin.status),
        venue_count=int(margin.venue_count),
        debt_count=int(margin.debt_count),
        venue_asset_indexes=[int(i) for i in margin.venue_asset_indexes],
        debt_asset_indexes=[int(i) for i in margin.debt_asset_indexes],
        event_sequence=int(margin.event_sequence),
        allowed_token_indexes=allowed,
        max_debts_per_margin=int(protocol.max_debts_per_margin),
        max_venues_per_margin=int(protocol.max_venues_per_margin),
        vault=balances,
    )
