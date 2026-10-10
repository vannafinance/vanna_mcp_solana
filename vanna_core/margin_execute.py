"""Shared `margin_execute` instruction builder - the margin account calling a whitelisted outside program
(Kamino Lend supply / redeem, Jupiter swaps), reviewed by the integration's VALIDATOR agent before Vanna signs
and health-checked by the oracle agent's prices afterwards. A port of the app's `marginExecuteIx`
(`lib/solana/exec.ts`) and of `instructions/account_manager/exec.rs` at layer origin/main `465f21a`.

Named accounts (program upgrade of 2026-10-09, the app's `marginExecuteIx` at backend `2462473`): authority,
protocol_config, margin_account, integration ["integration", target], target_program, validator, venue_asset (null
unless the call is AT a venue), event_authority ["__event_authority"], program (the core: events are emitted by CPI).
Args: `data` (the outside instruction's data), `call_account_count`. There is no `new_assets` any more: token collateral
is the protocol's allowed list, so a received token is already scanned, and one outside the list is refused.

remaining_accounts, in order:
  1. the outside instruction's accounts (none a signer: Vanna signs as the margin PDA);
  2. the health groups (`remaining_accounts.py`): a group per allowed token, per venue, per debt (nothing is excluded:
     the program values the post-call vault balances itself);
  3. the oracle segment `[vanna_oracle, price_book, ...price accounts]` of every holding and the spent/received tokens;
  4. the validator segment, only when the validator needs accounts (Phoenix: `[validator, market_book, exchange]`).

The receiving margin vault must exist before the call: an idempotent ATA create precedes the instruction (Vanna no
longer creates it). There is no `min_received`: slippage protection is the outside program's own (Jupiter's
`otherAmountThreshold`).
"""

from __future__ import annotations

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import create_idempotent_associated_token_account

from vanna_core.assets import AssetRegistry, DiscoveredAsset
from vanna_core.client import SolanaClient
from vanna_core.constants import EXEC_COMPUTE_UNITS  # noqa: F401 - re-exported
from vanna_core.layouts import EMPTY_ASSET_INDEX
from vanna_core.margin_state import load_margin_state
from vanna_core.pdas import integration_pda, protocol_config_pda
from vanna_core.remaining_accounts import held_collateral, oracle_segment, position_groups, refuse_venue, venue_keys_for
from vanna_core.vault_pricing import vault_oracle_keys

MARGIN_EXECUTE_DISC = bytes([71, 55, 105, 226, 51, 233, 66, 127])


def _borsh_vec_bytes(data: bytes) -> bytes:
    """Anchor `Vec<u8>` arg encoding: u32 length prefix + raw bytes."""
    return len(data).to_bytes(4, "little") + data


def _ro(key: Pubkey) -> AccountMeta:
    return AccountMeta(key, is_signer=False, is_writable=False)


def event_authority(program_id: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"__event_authority"], program_id)[0]


def _named_accounts(client: SolanaClient, authority: Pubkey, margin: Pubkey, target_program: Pubkey, venue_asset) -> list[AccountMeta]:
    protocol_config, _ = protocol_config_pda(client.program_id)
    integration, _ = integration_pda(target_program, client.program_id)
    return [
        AccountMeta(authority, is_signer=True, is_writable=True),
        _ro(protocol_config),
        AccountMeta(margin, is_signer=False, is_writable=True),
        _ro(integration),
        _ro(target_program),
        _ro(Pubkey.from_string(client.settings.vanna_validator_program_id)),
        # venue_asset: the venue's AssetConfig for a call AT a venue, else None (encoded as the program id).
        _ro(Pubkey.from_string(venue_asset.config_address)) if venue_asset is not None else _ro(client.program_id),
        _ro(event_authority(client.program_id)),
        _ro(client.program_id),
    ]


def _ix_data(data: bytes, call_account_count: int) -> bytes:
    return MARGIN_EXECUTE_DISC + _borsh_vec_bytes(data) + call_account_count.to_bytes(2, "little")


def _require_received_allowed(margin_view, received: DiscoveredAsset) -> None:
    allowed = getattr(margin_view, "allowed_token_indexes", None)
    if allowed is not None and received.asset_index not in allowed:
        raise ValueError(f"{received.symbol} is not on the protocol's allowed collateral list, so the margin account can't receive it")


async def build_margin_execute_instructions(
    client: SolanaClient,
    authority: Pubkey,
    margin: Pubkey,
    *,
    target_program: Pubkey,
    spent: DiscoveredAsset,
    received: DiscoveredAsset,
    cpi_accounts: list[AccountMeta],
    data: bytes,
    registry: AssetRegistry,
) -> list[Instruction]:
    """[idempotent create of the receiving vault, margin_execute]. Both go in one transaction, in this order."""
    margin_decoded = await load_margin_state(client, margin, registry)
    if spent.asset_index not in margin_decoded.held_collateral_indexes:
        raise ValueError(f"{spent.symbol} is not active collateral in the margin account, so it cannot be spent")
    if not received.collateral_enabled:
        raise ValueError(f"{received.symbol} is not enabled as collateral, so it cannot be received")
    for asset in (spent, received):
        refuse_venue(asset)
    _require_received_allowed(margin_decoded, received)

    create_received_vault = create_idempotent_associated_token_account(
        authority, margin, Pubkey.from_string(received.mint), Pubkey.from_string(received.token_program)
    )

    # Every health group, none excluded (the received token is already one of the allowed-token groups).
    groups = await position_groups(client, margin, margin_decoded, registry, set(), None)

    involved = [
        *[await registry.by_index(i) for i in dict.fromkeys(
            i for i in (*held_collateral(margin_decoded), *margin_decoded.debt_asset_indexes) if i != EMPTY_ASSET_INDEX
        )],
        spent,
        received,
    ]
    # A margin holding Phoenix perps hands the oracle the venue's accounts too, on every health-checked call.
    venue_keys = await venue_keys_for(client, margin, margin_decoded, registry)
    unique = list(dict.fromkeys(involved))
    segment = oracle_segment(client, unique, venue_keys, await vault_oracle_keys(client, unique))

    margin_execute = Instruction(
        program_id=client.program_id,
        data=_ix_data(data, len(cpi_accounts)),
        accounts=[*_named_accounts(client, authority, margin, target_program, None), *cpi_accounts, *groups, *segment],
    )
    return [create_received_vault, margin_execute]


# ── chained calls (Phoenix perps): one transaction, several margin_execute ─────────────────────────────


class MarginModel:
    """The margin's health lists and vault balances as the program will see them partway through a transaction, so a
    later `margin_execute` names the positions the earlier ones left (the app's `MarginIndexModel`, backend `2462473`).
    Allowed tokens are permanent groups (scanned whatever their balance); only venues join and leave the list."""

    def __init__(self, margin_decoded, vault_balances: dict[int, int]) -> None:
        self.allowed_token_indexes = list(getattr(margin_decoded, "allowed_token_indexes", []))
        self.venue_asset_indexes = list(getattr(margin_decoded, "venue_asset_indexes", []))
        self.debt_asset_indexes = list(margin_decoded.debt_asset_indexes)
        self.vault = {**getattr(margin_decoded, "vault", {}), **vault_balances}

    @property
    def collateral_asset_indexes(self) -> list[int]:
        return [*self.allowed_token_indexes, *self.venue_asset_indexes]

    @property
    def held_collateral_indexes(self) -> list[int]:
        tokens = [i for i in self.allowed_token_indexes if self.vault.get(i, 0) > 0]
        return [*tokens, *(i for i in self.venue_asset_indexes if i != EMPTY_ASSET_INDEX)]

    def _add(self, index: int) -> None:
        if index in self.allowed_token_indexes or index in self.venue_asset_indexes:
            return
        if EMPTY_ASSET_INDEX not in self.venue_asset_indexes:
            raise ValueError("The margin account already holds the maximum number of venues")
        self.venue_asset_indexes[self.venue_asset_indexes.index(EMPTY_ASSET_INDEX)] = index

    def _remove(self, index: int) -> None:
        if index in self.venue_asset_indexes:
            self.venue_asset_indexes[self.venue_asset_indexes.index(index)] = EMPTY_ASSET_INDEX

    def execute(self, spent: int | None, spent_amount: int, received: int | None, received_amount: int) -> None:
        """A drained spent vault leaves the collateral list; a newly funded received vault joins it."""
        if spent is not None:
            after = self.vault.get(spent, 0) - spent_amount
            self.vault[spent] = max(after, 0)
            if after <= 0:
                self._remove(spent)
        if received is not None:
            self.vault[received] = self.vault.get(received, 0) + received_amount
            if received_amount > 0:
                self._add(received)

    def venue_call(self, venue_index: int, opens: bool, closes: bool = False) -> None:
        """A call the validator marks `opens_venue` puts the venue on the list; one that empties it takes it off."""
        if opens:
            self._add(venue_index)
        if closes:
            self._remove(venue_index)

    def holds(self, index: int) -> bool:
        return index in self.held_collateral_indexes


async def build_chained_margin_execute(
    client: SolanaClient,
    authority: Pubkey,
    margin: Pubkey,
    model: MarginModel,
    registry: AssetRegistry,
    *,
    target_program: Pubkey,
    data: bytes,
    cpi_accounts: list[AccountMeta],
    spent: DiscoveredAsset | None = None,
    received: DiscoveredAsset | None = None,
    expected_spent: int = 0,
    expected_received: int = 0,
    venue_asset: DiscoveredAsset | None = None,
    venue=None,
    target_asset_ids: tuple[int, ...] = (),
    opens: bool = False,
    closes: bool = False,
    validator_accounts: list[Pubkey] | None = None,
) -> list[Instruction]:
    """One `margin_execute` built against `model` (not the chain), then replayed onto it - the app's `marginExecuteIx`.
    Returns [idempotent create of the receiving vault (if any), margin_execute].

    `venue_asset` + `venue` make it a call AT the Phoenix venue: the venue asset is named, the validator segment is
    appended, and the venue's accounts join the oracle segment even before it is on the collateral list, with
    `target_asset_ids` (the market an order targets)."""
    from vanna_core.phoenix import venue_oracle_keys

    out: list[Instruction] = []
    if spent is not None and not model.holds(spent.asset_index):
        raise ValueError(f"{spent.symbol} is not active collateral in the margin account, so it cannot be spent")
    if received is not None:
        if not received.collateral_enabled:
            raise ValueError(f"{received.symbol} is not enabled as collateral, so it cannot be received")
        _require_received_allowed(model, received)
        mint, program = Pubkey.from_string(received.mint), Pubkey.from_string(received.token_program)
        out.append(create_idempotent_associated_token_account(authority, margin, mint, program))

    groups = await position_groups(client, margin, model, registry, set(), None)
    active = [await registry.by_index(i) for i in dict.fromkeys(
        i for i in (*model.held_collateral_indexes, *model.debt_asset_indexes) if i != EMPTY_ASSET_INDEX
    )]
    priced = [a for a in (spent, received) if a is not None]
    involved = list(dict.fromkeys([*priced, *active]))
    holds_venue = any(a.is_venue for a in active)
    venue_keys: list[str] = []
    if venue_asset is not None or holds_venue:
        if venue is None:
            # Any call on a margin that holds Phoenix must hand the oracle the venue's accounts: read it if the caller
            # didn't pass it, rather than refusing.
            from vanna_core.phoenix import load_phoenix_venue

            venue = await load_phoenix_venue(client, margin, await client.get_chain_clock_unix_timestamp())
            if venue is None:
                raise ValueError("This margin account holds Phoenix perps, but Vanna's Phoenix market book was not found")
        venue_keys = venue_oracle_keys(margin, venue, list(target_asset_ids) if venue_asset is not None else [])
    segment = oracle_segment(client, involved, venue_keys, await vault_oracle_keys(client, involved))
    validator = Pubkey.from_string(client.settings.vanna_validator_program_id)
    validator_segment = [_ro(validator), *(_ro(k) for k in validator_accounts)] if validator_accounts else []

    named = _named_accounts(client, authority, margin, target_program, venue_asset)
    cpi = [AccountMeta(a.pubkey, is_signer=False, is_writable=a.is_writable) for a in cpi_accounts]
    out.append(Instruction(client.program_id, _ix_data(data, len(cpi)), [*named, *cpi, *groups, *segment, *validator_segment]))

    model.execute(
        spent.asset_index if spent is not None else None, expected_spent,
        received.asset_index if received is not None else None, expected_received,
    )
    if venue_asset is not None:
        model.venue_call(venue_asset.asset_index, opens, closes)
    return out
