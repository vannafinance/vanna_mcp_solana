"""Wallet-side token preparation: idempotent ATA creation, and native-SOL
wrapping into wSOL. Every write action that pulls funds from the caller's own
wallet (Earn supply, collateral deposit) prepends these — the Vanna program
only ever auto-creates its *own* PDAs' token accounts (margin vaults, the
lender's share account, the liquidator's collateral ATA), never the caller's
wallet-side accounts (TOOLS_CATALOGUE.md §2.2.3).
"""

from __future__ import annotations

from solders.instruction import Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from spl.token.instructions import SyncNativeParams, create_idempotent_associated_token_account, sync_native

from vanna_core.pdas import find_associated_token_address


def wallet_prep_instructions(
    *,
    payer: Pubkey,
    owner: Pubkey,
    mint: Pubkey,
    token_program: Pubkey,
    is_native: bool = False,
    wrap_lamports: int = 0,
) -> list[Instruction]:
    """Idempotent-create `owner`'s ATA for `mint` (funded by `payer`), and for
    native SOL, fund it with `wrap_lamports` and sync so the wrapped-SOL
    balance actually reflects the transfer. Safe to call even when the ATA
    already exists and already holds enough — `wrap_lamports=0` skips the
    wrap step entirely."""
    ata = find_associated_token_address(owner, mint, token_program)
    instructions: list[Instruction] = [
        create_idempotent_associated_token_account(payer, owner, mint, token_program)
    ]
    if is_native and wrap_lamports > 0:
        instructions.append(
            transfer(TransferParams(from_pubkey=payer, to_pubkey=ata, lamports=wrap_lamports))
        )
        instructions.append(sync_native(SyncNativeParams(program_id=token_program, account=ata)))
    return instructions
