"""Shared `margin_execute` instruction builder — the named accounts every
adapter call needs (protocol_config, margin_account, integration,
target_program, spent/received asset+mint+vault, token programs), then
remaining_accounts = CPI accounts + position groups + oracle accounts. The CPI-specific
accounts (Kamino's 12, Jupiter's variable-length route) are built by each
adapter's own module (`farm_kamino.py`, `swap.py`) and passed in.
"""

from __future__ import annotations

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from vanna_core.assets import AssetRegistry, DiscoveredAsset
from vanna_core.client import SolanaClient
from vanna_core.layouts import MarginAccount as MarginAccountLayout
from vanna_core.pdas import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    find_associated_token_address,
    integration_pda,
    protocol_config_pda,
)
from vanna_core.remaining_accounts import build_health_accounts

SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
MARGIN_EXECUTE_DISC = bytes([71, 55, 105, 226, 51, 233, 66, 127])


def _borsh_vec_bytes(data: bytes) -> bytes:
    """Anchor `Vec<u8>` arg encoding: u32 length prefix + raw bytes."""
    return len(data).to_bytes(4, "little") + data


async def build_margin_execute_instruction(
    client: SolanaClient,
    authority: Pubkey,
    margin: Pubkey,
    *,
    target_program: Pubkey,
    spent: DiscoveredAsset,
    received: DiscoveredAsset,
    cpi_accounts: list[AccountMeta],
    data: bytes,
    min_received: int,
    registry: AssetRegistry,
) -> Instruction:
    margin_decoded = await client.fetch_program_account(margin, "MarginAccount", MarginAccountLayout)
    remaining = await build_health_accounts(
        client, margin, margin_decoded, registry,
        exclude_collateral_indexes={spent.asset_index, received.asset_index}, exclude_debt_index=None,
        extra_assets=[spent, received],
        skip_oracle_keys={str(a.pubkey) for a in cpi_accounts},
    )

    protocol_config, _ = protocol_config_pda(client.program_id)
    integration, _ = integration_pda(target_program, client.program_id)
    spent_vault = find_associated_token_address(margin, Pubkey.from_string(spent.mint), Pubkey.from_string(spent.token_program))
    received_vault = find_associated_token_address(margin, Pubkey.from_string(received.mint), Pubkey.from_string(received.token_program))

    named_accounts = [
        AccountMeta(authority, is_signer=True, is_writable=True),
        AccountMeta(protocol_config, is_signer=False, is_writable=False),
        AccountMeta(margin, is_signer=False, is_writable=True),
        AccountMeta(integration, is_signer=False, is_writable=False),
        AccountMeta(target_program, is_signer=False, is_writable=False),
        AccountMeta(Pubkey.from_string(spent.config_address), is_signer=False, is_writable=False),
        AccountMeta(Pubkey.from_string(spent.mint), is_signer=False, is_writable=False),
        AccountMeta(spent_vault, is_signer=False, is_writable=True),
        AccountMeta(Pubkey.from_string(received.config_address), is_signer=False, is_writable=False),
        AccountMeta(Pubkey.from_string(received.mint), is_signer=False, is_writable=False),
        AccountMeta(received_vault, is_signer=False, is_writable=True),
        AccountMeta(Pubkey.from_string(spent.token_program), is_signer=False, is_writable=False),
        AccountMeta(Pubkey.from_string(received.token_program), is_signer=False, is_writable=False),
        AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]

    ix_data = (
        MARGIN_EXECUTE_DISC
        + _borsh_vec_bytes(data)
        + len(cpi_accounts).to_bytes(2, "little")
        + min_received.to_bytes(8, "little")
    )
    return Instruction(
        program_id=client.program_id,
        data=ix_data,
        accounts=[*named_accounts, *cpi_accounts, *remaining],
    )
