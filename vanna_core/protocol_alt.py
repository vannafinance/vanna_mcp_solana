"""The protocol address lookup table: the accounts every margin build repeats for every user.

A margin instruction names the protocol config, the integration and target program, each involved asset's
config / mint / token program / reserve, and every oracle account those assets read. Written out in full that
is about 26 keys x 32 bytes; with a Jupiter route on top it overflows Solana's 1232-byte packet for routes into
liquid-staking tokens and for accounts holding several assets. Loading those fixed keys from one lookup table
costs one byte each instead.

Only protocol-wide, non-signer keys go in. Per-user keys (the wallet, its margin account, vaults and debt
positions) stay static. The table is created and frozen once by `scripts/create_protocol_lookup_table.py`;
builds use it when `PROTOCOL_LOOKUP_TABLES` names it, and skip it if it is missing (for example after a fork
reset), so a stale setting can never break a build.

Only margin swaps opt in (`build_write_envelope(use_protocol_lookup_tables=True)`). An account loaded from a
lookup table is not indexed by the fork's getSignaturesForAddress, and Earn history finds a lender's
deposits by the reserve address; loading reserves in Earn transactions made earned-interest read 0.
"""

from __future__ import annotations

import logging
import struct

from solders.address_lookup_table_account import AddressLookupTable, AddressLookupTableAccount
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from vanna_core.assets import (
    JUPITER_V6_PROGRAM_ID,
    KAMINO_INSTRUCTIONS_SYSVAR,
    KAMINO_MAIN_MARKET,
    KAMINO_MAIN_MARKET_AUTHORITY,
    KAMINO_RECEIPTS,
    KLEND_PROGRAM_ID,
    PYTH_PUSH_ORACLE_PROGRAM_ID,
    PYTH_RECEIVER_PROGRAM_ID,
    AssetRegistry,
)
from vanna_core.pdas import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID, integration_pda, protocol_config_pda

logger = logging.getLogger(__name__)

LOOKUP_TABLE_PROGRAM_ID = Pubkey.from_string("AddressLookupTab1e1111111111111111111111111")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
MAX_TABLE_ADDRESSES = 256


async def protocol_lookup_addresses(program_id: Pubkey, registry: AssetRegistry) -> list[Pubkey]:
    """Every protocol-wide key a margin build may name, de-duplicated, in a stable order."""
    keys: list[Pubkey] = [
        program_id,
        protocol_config_pda(program_id)[0],
        integration_pda(JUPITER_V6_PROGRAM_ID, program_id)[0],
        integration_pda(KLEND_PROGRAM_ID, program_id)[0],
        JUPITER_V6_PROGRAM_ID,
        KLEND_PROGRAM_ID,
        KAMINO_MAIN_MARKET,
        KAMINO_MAIN_MARKET_AUTHORITY,
        KAMINO_INSTRUCTIONS_SYSVAR,
        PYTH_RECEIVER_PROGRAM_ID,
        PYTH_PUSH_ORACLE_PROGRAM_ID,
        TOKEN_PROGRAM_ID,
        TOKEN_2022_PROGRAM_ID,
        ASSOCIATED_TOKEN_PROGRAM_ID,
        SYSTEM_PROGRAM_ID,
    ]
    for asset in registry.all(include_unoffered=True):
        keys.append(Pubkey.from_string(asset.config_address))
        keys.append(Pubkey.from_string(asset.mint))
        keys.append(Pubkey.from_string(asset.token_program))
        if asset.has_reserve:
            keys.append(Pubkey.from_string(asset.reserve))
        keys.extend(Pubkey.from_string(k) for k in asset.oracle.accounts())
    for receipt in KAMINO_RECEIPTS:
        keys += [receipt.reserve_pubkey, Pubkey.from_string(receipt.liquidity_vault), receipt.collateral_mint_pubkey]

    seen: set[Pubkey] = set()
    ordered: list[Pubkey] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    if len(ordered) > MAX_TABLE_ADDRESSES:
        raise ValueError(f"{len(ordered)} protocol addresses exceed one lookup table ({MAX_TABLE_ADDRESSES})")
    return ordered


# ── lookup-table program instructions (bincode: u32 variant, then fields) ─────────────────────────────

def derive_table_address(authority: Pubkey, recent_slot: int) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address([bytes(authority), struct.pack("<Q", recent_slot)], LOOKUP_TABLE_PROGRAM_ID)


def create_table_ix(authority: Pubkey, payer: Pubkey, recent_slot: int) -> tuple[Instruction, Pubkey]:
    table, bump = derive_table_address(authority, recent_slot)
    data = struct.pack("<IQB", 0, recent_slot, bump)
    accounts = [
        AccountMeta(table, is_signer=False, is_writable=True),
        AccountMeta(authority, is_signer=True, is_writable=False),
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(LOOKUP_TABLE_PROGRAM_ID, data, accounts), table


def extend_table_ix(table: Pubkey, authority: Pubkey, payer: Pubkey, addresses: list[Pubkey]) -> Instruction:
    data = struct.pack("<IQ", 2, len(addresses)) + b"".join(bytes(a) for a in addresses)
    accounts = [
        AccountMeta(table, is_signer=False, is_writable=True),
        AccountMeta(authority, is_signer=True, is_writable=False),
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(LOOKUP_TABLE_PROGRAM_ID, data, accounts)


def freeze_table_ix(table: Pubkey, authority: Pubkey) -> Instruction:
    accounts = [
        AccountMeta(table, is_signer=False, is_writable=True),
        AccountMeta(authority, is_signer=True, is_writable=False),
    ]
    return Instruction(LOOKUP_TABLE_PROGRAM_ID, struct.pack("<I", 1), accounts)


# ── loading the configured tables for builds ─────────────────────────────────────────────────────────

_loaded: dict[str, AddressLookupTableAccount] = {}


def configured_table_addresses(setting: str) -> list[str]:
    return [a.strip() for a in setting.split(",") if a.strip()]


async def load_protocol_tables(client, setting: str) -> list[AddressLookupTableAccount]:
    """The configured protocol tables, fetched once per process. A table that cannot be read is skipped
    (and retried on the next build), never fatal."""
    tables: list[AddressLookupTableAccount] = []
    for address in configured_table_addresses(setting):
        cached = _loaded.get(address)
        if cached is None:
            try:
                key = Pubkey.from_string(address)
                data = await client.fetch_raw_account(key)
                cached = AddressLookupTableAccount(key=key, addresses=AddressLookupTable.deserialize(bytes(data)).addresses)
                _loaded[address] = cached
            except Exception as exc:  # noqa: BLE001
                logger.warning("protocol lookup table %s unavailable, building without it: %s", address, exc)
                continue
        tables.append(cached)
    return tables


def clear_protocol_table_cache() -> None:
    _loaded.clear()
