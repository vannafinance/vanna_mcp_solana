"""Shared `margin_execute` instruction builder - the margin account calling a whitelisted outside program
(Kamino Lend supply / redeem, Jupiter swaps), reviewed by the integration's VALIDATOR agent before Vanna signs
and health-checked by the oracle agent's prices afterwards. A port of the app's `marginExecuteIx`
(`lib/solana/exec.ts`) and of `instructions/account_manager/exec.rs` at layer origin/main `465f21a`.

Named accounts: authority, protocol_config, margin_account, integration ["integration", target], target_program,
validator, venue_asset (null), and venue_account (null) only for the program build that still has it
(`VannaSettings.margin_execute_venue_account`; the fork's current build, feature/phoenix-titan, removed it). Args: `data` (the outside instruction's data),
`call_account_count`, `new_assets` (u8: tokens received for the first time).

remaining_accounts, in order:
  1. the outside instruction's accounts (none a signer: Vanna signs as the margin PDA);
  2. EVERY position group of the margin, in slot order (nothing is excluded: the program values the post-call
     vault balances itself);
  3. one `[asset_config, margin_vault]` group per token received for the first time (`new_assets`);
  4. the oracle segment `[vanna_oracle, price_book, ...price accounts]` of every holding and every new token;
  5. the validator segment, only when the validator needs accounts (GMTrade: its market book) - never for
     Kamino or Jupiter.

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
from vanna_core.layouts import EMPTY_ASSET_INDEX
from vanna_core.layouts import MarginAccount as MarginAccountLayout
from vanna_core.pdas import find_associated_token_address, integration_pda, protocol_config_pda
from vanna_core.remaining_accounts import oracle_segment, position_groups, refuse_venue, venue_keys_for

MARGIN_EXECUTE_DISC = bytes([71, 55, 105, 226, 51, 233, 66, 127])
# Kamino and Jupiter use ~1M in the Frontend Integration Guide, a Phoenix deposit + order up to ~600k: one budget for all.
EXEC_COMPUTE_UNITS = 1_400_000


def _borsh_vec_bytes(data: bytes) -> bytes:
    """Anchor `Vec<u8>` arg encoding: u32 length prefix + raw bytes."""
    return len(data).to_bytes(4, "little") + data


def _ro(key: Pubkey) -> AccountMeta:
    return AccountMeta(key, is_signer=False, is_writable=False)


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
    margin_decoded = await client.fetch_program_account(margin, "MarginAccount", MarginAccountLayout)
    if spent.asset_index not in margin_decoded.collateral_asset_indexes:
        raise ValueError(f"{spent.symbol} is not active collateral in the margin account, so it cannot be spent")
    if not received.collateral_enabled:
        raise ValueError(f"{received.symbol} is not enabled as collateral, so it cannot be received")
    for asset in (spent, received):
        refuse_venue(asset)

    received_vault = find_associated_token_address(margin, Pubkey.from_string(received.mint), Pubkey.from_string(received.token_program))
    create_received_vault = create_idempotent_associated_token_account(
        authority, margin, Pubkey.from_string(received.mint), Pubkey.from_string(received.token_program)
    )

    # Every position group, none excluded; a token received for the first time adds one more group.
    groups = await position_groups(client, margin, margin_decoded, registry, set(), None)
    is_new = received.asset_index not in margin_decoded.collateral_asset_indexes
    new_groups = [_ro(Pubkey.from_string(received.config_address)), _ro(received_vault)] if is_new else []

    involved = [
        *[await registry.by_index(i) for i in dict.fromkeys(
            i for i in (*margin_decoded.collateral_asset_indexes, *margin_decoded.debt_asset_indexes) if i != EMPTY_ASSET_INDEX
        )],
        spent,
        received,
    ]
    # A margin holding Phoenix perps hands the oracle the venue's accounts too, on every health-checked call.
    venue_keys = await venue_keys_for(client, margin, margin_decoded, registry)
    segment = oracle_segment(client, list(dict.fromkeys(involved)), venue_keys)

    protocol_config, _ = protocol_config_pda(client.program_id)
    integration, _ = integration_pda(target_program, client.program_id)
    validator = Pubkey.from_string(client.settings.vanna_validator_program_id)
    named_accounts = [
        AccountMeta(authority, is_signer=True, is_writable=True),
        _ro(protocol_config),
        AccountMeta(margin, is_signer=False, is_writable=True),
        _ro(integration),
        _ro(target_program),
        _ro(validator),
        _ro(client.program_id),  # venue_asset: None (optional account encoded as the program id)
    ]
    # venue_account exists only in the GMTrade build of the program (see VannaSettings.margin_execute_venue_account).
    if client.settings.margin_execute_venue_account:
        named_accounts.append(_ro(client.program_id))  # venue_account: None
    ix_data = MARGIN_EXECUTE_DISC + _borsh_vec_bytes(data) + len(cpi_accounts).to_bytes(2, "little") + bytes([1 if is_new else 0])
    margin_execute = Instruction(
        program_id=client.program_id,
        data=ix_data,
        accounts=[*named_accounts, *cpi_accounts, *groups, *new_groups, *segment],
    )
    return [create_received_vault, margin_execute]


# ── chained calls (Phoenix perps): one transaction, several margin_execute ─────────────────────────────


class MarginModel:
    """The margin's active-asset lists and vault balances as the program will see them partway through a transaction,
    so a later `margin_execute` names the positions the earlier ones left (the app's `MarginIndexModel`)."""

    def __init__(self, margin_decoded, vault_balances: dict[int, int]) -> None:
        self.collateral_asset_indexes = list(margin_decoded.collateral_asset_indexes)
        self.debt_asset_indexes = list(margin_decoded.debt_asset_indexes)
        self.vault = dict(vault_balances)

    def _add(self, index: int) -> None:
        if index in self.collateral_asset_indexes:
            return
        if EMPTY_ASSET_INDEX not in self.collateral_asset_indexes:
            raise ValueError("The margin account already holds the maximum number of active assets")
        self.collateral_asset_indexes[self.collateral_asset_indexes.index(EMPTY_ASSET_INDEX)] = index

    def _remove(self, index: int) -> None:
        if index in self.collateral_asset_indexes:
            self.collateral_asset_indexes[self.collateral_asset_indexes.index(index)] = EMPTY_ASSET_INDEX

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
        return index in self.collateral_asset_indexes


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
    new_groups: list[AccountMeta] = []
    if received is not None:
        if not received.collateral_enabled:
            raise ValueError(f"{received.symbol} is not enabled as collateral, so it cannot be received")
        mint, program = Pubkey.from_string(received.mint), Pubkey.from_string(received.token_program)
        out.append(create_idempotent_associated_token_account(authority, margin, mint, program))
        if not model.holds(received.asset_index):
            new_groups = [_ro(Pubkey.from_string(received.config_address)), _ro(find_associated_token_address(margin, mint, program))]

    groups = await position_groups(client, margin, model, registry, set(), None)
    active = [await registry.by_index(i) for i in dict.fromkeys(
        i for i in (*model.collateral_asset_indexes, *model.debt_asset_indexes) if i != EMPTY_ASSET_INDEX
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
    segment = oracle_segment(client, involved, venue_keys)
    validator = Pubkey.from_string(client.settings.vanna_validator_program_id)
    validator_segment = [_ro(validator), *(_ro(k) for k in validator_accounts)] if validator_accounts else []

    protocol_config, _ = protocol_config_pda(client.program_id)
    integration, _ = integration_pda(target_program, client.program_id)
    named = [
        AccountMeta(authority, is_signer=True, is_writable=True),
        _ro(protocol_config),
        AccountMeta(margin, is_signer=False, is_writable=True),
        _ro(integration),
        _ro(target_program),
        _ro(validator),
        # venue_asset: the Phoenix venue's AssetConfig for a call at Phoenix, else None (encoded as the program id).
        _ro(Pubkey.from_string(venue_asset.config_address)) if venue_asset is not None else _ro(client.program_id),
    ]
    if client.settings.margin_execute_venue_account:
        named.append(_ro(client.program_id))
    cpi = [AccountMeta(a.pubkey, is_signer=False, is_writable=a.is_writable) for a in cpi_accounts]
    ix_data = MARGIN_EXECUTE_DISC + _borsh_vec_bytes(data) + len(cpi).to_bytes(2, "little") + bytes([1 if new_groups else 0])
    out.append(Instruction(client.program_id, ix_data, [*named, *cpi, *groups, *new_groups, *segment, *validator_segment]))

    model.execute(
        spent.asset_index if spent is not None else None, expected_spent,
        received.asset_index if received is not None else None, expected_received,
    )
    if venue_asset is not None:
        model.venue_call(venue_asset.asset_index, opens, closes)
    return out
