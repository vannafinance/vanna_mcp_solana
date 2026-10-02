"""Offline tests for the protocol lookup table (vanna_core/protocol_alt.py) and the margin-swap size check."""

from __future__ import annotations

import struct

import pytest
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from vanna_core.contracts.swap import MAX_TX_BYTES, fits_one_transaction
from vanna_core.protocol_alt import (
    LOOKUP_TABLE_PROGRAM_ID,
    clear_protocol_table_cache,
    configured_table_addresses,
    create_table_ix,
    derive_table_address,
    extend_table_ix,
    freeze_table_ix,
    load_protocol_tables,
)

AUTH = Keypair().pubkey()


def test_create_instruction_encodes_slot_and_bump_and_derives_the_table():
    ix, table = create_table_ix(AUTH, AUTH, 123)
    expected, bump = derive_table_address(AUTH, 123)
    assert table == expected
    assert ix.program_id == LOOKUP_TABLE_PROGRAM_ID
    assert bytes(ix.data) == struct.pack("<IQB", 0, 123, bump)
    assert ix.accounts[0].pubkey == table and ix.accounts[0].is_writable
    assert ix.accounts[1].is_signer and ix.accounts[2].is_signer


def test_extend_instruction_carries_every_address():
    addrs = [Keypair().pubkey() for _ in range(3)]
    ix = extend_table_ix(AUTH, AUTH, AUTH, addrs)
    data = bytes(ix.data)
    assert data[:12] == struct.pack("<IQ", 2, 3)
    assert data[12:] == b"".join(bytes(a) for a in addrs)


def test_freeze_instruction():
    ix = freeze_table_ix(AUTH, AUTH)
    assert bytes(ix.data) == struct.pack("<I", 1)


def test_setting_parsing():
    assert configured_table_addresses("") == []
    assert configured_table_addresses(" a , b ,") == ["a", "b"]


@pytest.mark.asyncio
async def test_a_missing_table_is_skipped_not_fatal():
    clear_protocol_table_cache()

    class Missing:
        async def fetch_raw_account(self, _key):
            raise RuntimeError("account not found")

    assert await load_protocol_tables(Missing(), str(Keypair().pubkey())) == []


def _ix(n_accounts: int) -> Instruction:
    metas = [AccountMeta(Keypair().pubkey(), is_signer=False, is_writable=False) for _ in range(n_accounts)]
    return Instruction(Pubkey.from_string("H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC"), b"\x00" * 16, metas)


def test_size_check_accepts_a_small_transaction_and_rejects_an_oversized_one():
    assert fits_one_transaction([_ix(5)], AUTH, [], 1_000_000)
    # ~40 written-out keys x 32 bytes is well over the 1232-byte packet.
    assert not fits_one_transaction([_ix(40)], AUTH, [], 1_000_000)
    assert MAX_TX_BYTES == 1232
