"""PDA derivation for vanna_lending — ported 1:1 from Backend-Solana's
lib/solana/pdas.ts. Same seeds, same program, same order.
"""

from __future__ import annotations

from solders.pubkey import Pubkey


def protocol_config_pda(program_id: Pubkey) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address([b"protocol"], program_id)


def asset_config_pda(mint: Pubkey, program_id: Pubkey) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address([b"asset", bytes(mint)], program_id)


def reserve_pda(mint: Pubkey, program_id: Pubkey) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address([b"reserve", bytes(mint)], program_id)


def share_mint_pda(mint: Pubkey, program_id: Pubkey) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address([b"share_mint", bytes(mint)], program_id)


def margin_pda(authority: Pubkey, program_id: Pubkey) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address([b"margin", bytes(authority)], program_id)


def debt_position_pda(
    margin: Pubkey, reserve: Pubkey, program_id: Pubkey
) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address(
        [b"debt", bytes(margin), bytes(reserve)], program_id
    )
