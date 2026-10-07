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
from vanna_core.remaining_accounts import oracle_segment, position_groups
from vanna_core.types import UnpricablePositionError

MARGIN_EXECUTE_DISC = bytes([71, 55, 105, 226, 51, 233, 66, 127])
# Kamino and Jupiter use ~1M in the Frontend Integration Guide, GMTrade orders 1.4M: one budget for all.
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
        if asset.is_venue:
            raise UnpricablePositionError("Venue assets cannot be used in a token call.")

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
    segment = oracle_segment(client, list(dict.fromkeys(involved)))

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
