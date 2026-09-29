"""PDA derivation for vanna_lending — ported 1:1 from vanna-credit-backend's
lib/solana/pdas.ts. Same seeds, same program, same order. Verified against
`Vanna-Solana-Copilot-Answers.pdf` (2026-09-28) and TOOLS_CATALOGUE.md §1.
"""

from __future__ import annotations

from solders.pubkey import Pubkey

ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

# Pyth push-oracle shard: every sponsored feed lives in shard 0 — two leading
# zero bytes ahead of the 32-byte feed ID (verified, `lib/solana/pyth.ts` /
# `lib/solana/config.ts` PYTH_SHARD_ID).
PYTH_SHARD_ID = b"\x00\x00"


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


def integration_pda(external_program_id: Pubkey, program_id: Pubkey) -> tuple[Pubkey, int]:
    """Whitelist entry PDA for one external program `margin_execute` may call
    (Kamino Lend or Jupiter today). Seeds: `["integration", external_program_id]`."""
    return Pubkey.find_program_address(
        [b"integration", bytes(external_program_id)], program_id
    )


def price_feed_account(feed_id: str, push_oracle_program_id: Pubkey) -> tuple[Pubkey, int]:
    """The Pyth `PriceUpdateV2` address a fresh Hermes update is posted to /
    read from: PDA(shard-0 bytes + 32-byte feed id, push-oracle program).
    `feed_id` is hex, with or without a leading "0x".

    Don't confuse three different things with similar shapes: the hex feed
    ID (an opaque identifier), this derived account address (where the price
    actually lives), and the push-oracle program ID (who derives/owns it).
    """
    feed_id_bytes = bytes.fromhex(feed_id.removeprefix("0x"))
    if len(feed_id_bytes) != 32:
        raise ValueError(f"Pyth feed id must be 32 bytes, got {len(feed_id_bytes)}: {feed_id!r}")
    return Pubkey.find_program_address(
        [PYTH_SHARD_ID, feed_id_bytes], push_oracle_program_id
    )


def find_associated_token_address(
    owner: Pubkey, mint: Pubkey, token_program: Pubkey = TOKEN_PROGRAM_ID
) -> Pubkey:
    """Standard ATA derivation, explicit token program (Token-2022 support is
    unused by the real registry today, but the derivation still needs the
    right program id to be correct if that ever changes). Works identically
    whether `owner` is a wallet or a PDA (e.g. a margin/reserve account) —
    Anchor's ATA seeds don't distinguish the two cases; `allowOwnerOffCurve`
    is a client-side check only, not part of the derivation."""
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]
