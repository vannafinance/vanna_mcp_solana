"""The per-wallet address lookup table: since the 2026-10-09 upgrade every health-checked margin transaction names a
`[asset_config, margin_vault]` group for each of the protocol's allowed tokens (23 today), so a borrow alone is ~1.4 KB
with the protocol table - over Solana's 1232-byte packet. The per-wallet keys (the margin's vaults, debt positions, the
Phoenix trader) can't go in the shared protocol table, so, like the app (`prepareTransactionTables` in
`lib/solana/tx.ts`, backend `2462473`), a wallet gets its own table.

Differences from the app, for a server: the app keeps the table in browser memory and makes a new one after a reload;
here one table per wallet is kept and extended. Its address is remembered in-process and in the MCP's state store, and
the setup transaction carries a memo naming it (`vanna-alt:<table>`), so any instance can find it again from the
wallet's own history (one `getSignaturesForAddress` call, which returns memos) after a restart. Anyone can send a
transaction that touches a wallet and carries such a memo, so a memo counts only when the wallet itself paid for that
transaction (one `getTransaction` per candidate, at most `_MEMO_CHECKS`). Creating or extending is
ONE setup transaction returned in place of the action, flagged
`summary["prerequisite"] = True`. The wallet signs it; then the same action is built again and fits.

The margin account itself never goes in: the fork does not index accounts loaded from a lookup table in
`getSignaturesForAddress`, and margin history is found by the margin account's address (see `protocol_alt.py`).
"""

from __future__ import annotations

import logging
import time

from solders.address_lookup_table_account import AddressLookupTable, AddressLookupTableAccount
from solders.instruction import Instruction
from solders.pubkey import Pubkey

from vanna_core.constants import MAX_TABLE_ADDRESSES, MAX_TX_BYTES  # noqa: F401 - re-exported
from vanna_core.protocol_alt import create_table_ix, extend_table_ix

logger = logging.getLogger(__name__)

# One setup transaction: create + extend with up to this many keys still fits in a packet (~269 + 32 bytes per key).
MAX_KEYS_PER_SETUP = 30
# A setup the MCP built but hasn't seen land may never be signed (the Copilot's dry build simulates before approval).
# Rebuilding it reuses the same creation slot - so the same table address - while that slot is still recent (the
# program accepts slots in SlotHashes, 512 deep); a copy signed twice then fails harmlessly instead of making two tables.
_REUSE_SLOT_WITHIN = 300
_pending_create: dict[str, tuple[str, int]] = {}  # wallet -> (table, creation slot) of a create not yet seen on chain
_STORE_TTL_SECONDS = 400 * 24 * 3600.0

_tables: dict[str, tuple[str, float]] = {}  # wallet -> (table address, recorded at)
MEMO_PROGRAM_ID = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
MEMO_PREFIX = "vanna-alt:"
_HISTORY_SCAN = 1000  # signatures read (one RPC call) when looking for the table's setup memo
_MEMO_CHECKS = 5  # memo transactions checked for their fee payer; spam memos beyond this just mean a new setup
_store = None  # the MCP's RecordStore (in-memory or Firestore), set at start-up; optional


def set_wallet_table_store(store) -> None:
    global _store
    _store = store


def _key(wallet: Pubkey) -> str:
    return f"wallet_alt:{wallet}"


async def _remember(wallet: Pubkey, table: Pubkey) -> None:
    _tables[str(wallet)] = (str(table), time.time())
    if _store is not None:
        try:
            await _store.put(_key(wallet), {"table": str(table), "at": time.time()}, _STORE_TTL_SECONDS)
        except Exception:  # noqa: BLE001 - the in-process copy still works for this instance
            logger.warning("could not save the wallet lookup table for %s", wallet, exc_info=True)


async def _from_history(client, wallet: Pubkey) -> str | None:
    """The most recent table named by a setup memo in a transaction the wallet itself paid for."""
    try:
        signatures = await client.get_signatures_for_address(wallet, limit=_HISTORY_SCAN)
    except Exception:  # noqa: BLE001 - discovery is best-effort: a missing table just means a new setup
        return None
    checks = 0
    for entry in signatures:
        memo = getattr(entry, "memo", None) or ""
        if entry.err is not None or MEMO_PREFIX not in memo:
            continue
        address = memo.split(MEMO_PREFIX, 1)[1].split()[0].strip(" ;,]\"'")
        try:
            Pubkey.from_string(address)
        except ValueError:
            continue
        if checks >= _MEMO_CHECKS:
            return None
        checks += 1
        try:
            payer = await client.get_transaction_fee_payer(entry.signature)
        except Exception:  # noqa: BLE001
            continue
        if payer == str(wallet):  # someone else's memo naming a table for this wallet is ignored
            return address
    return None


async def _recall(wallet: Pubkey, client=None) -> tuple[str, float] | None:
    hit = _tables.get(str(wallet))
    if hit is not None:
        return hit
    if _store is not None:
        try:
            rec = await _store.get(_key(wallet))
            if rec and rec.get("table"):
                hit = (str(rec["table"]), float(rec.get("at") or 0))
                _tables[str(wallet)] = hit
                return hit
        except Exception:  # noqa: BLE001
            logger.warning("could not read the wallet lookup table for %s", wallet, exc_info=True)
    if client is not None:
        address = await _from_history(client, wallet)
        if address is not None:
            hit = (address, 0.0)  # found on chain: already landed, no grace period
            await _remember(wallet, Pubkey.from_string(address))
            _tables[str(wallet)] = hit
            return hit
    return None


class WalletTableNotReady(Exception):
    """Kept for callers that catch it; a setup that hasn't landed no longer raises (see `_pending_create`)."""


async def load_wallet_table(client, wallet: Pubkey) -> AddressLookupTableAccount | None:
    """The wallet's table if one is known and live (owned by this wallet); None otherwise - including a setup that was
    built but hasn't landed (or was never signed): the next build returns the same setup again (`setup_instructions`)."""
    hit = await _recall(wallet, client)
    if hit is None:
        return None
    address, _ = hit
    key = Pubkey.from_string(address)
    data = await client.fetch_raw_account_or_none(key)
    if data is None:
        # The remembered table never landed (an unsigned setup). A table the wallet did set up earlier is still found
        # from its own history.
        found = await _from_history(client, wallet)
        if found is None or found == address:
            return None
        key = Pubkey.from_string(found)
        data = await client.fetch_raw_account_or_none(key)
        if data is None:
            return None
        await _remember(wallet, key)
    _pending_create.pop(str(wallet), None)
    table = AddressLookupTable.deserialize(bytes(data))
    if table.meta.authority != wallet:
        return None
    return AddressLookupTableAccount(key=key, addresses=list(table.addresses))


def wanted_keys(instructions: list[Instruction], payer: Pubkey, tables: list[AddressLookupTableAccount],
                keep_static: set[Pubkey]) -> list[Pubkey]:
    """Every non-signer key of these instructions that no loaded table holds (the app's rule), minus `keep_static`."""
    seen = {k for t in tables for k in t.addresses}
    out: list[Pubkey] = []
    for ix in instructions:
        signers = {m.pubkey for m in ix.accounts if m.is_signer}
        for key in [ix.program_id, *(m.pubkey for m in ix.accounts)]:
            if key in seen or key == payer or key in signers or key in keep_static:
                continue
            seen.add(key)
            out.append(key)
    return out


async def setup_instructions(client, wallet: Pubkey, existing: AddressLookupTableAccount | None,
                             keys: list[Pubkey]) -> tuple[list[Instruction], Pubkey]:
    """One transaction's worth: create + extend (new table) or extend (existing), up to MAX_KEYS_PER_SETUP keys. Keys
    beyond that are added by the next setup, if the action still doesn't fit."""
    batch = keys[:MAX_KEYS_PER_SETUP]
    if existing is not None and len(existing.addresses) + len(batch) <= MAX_TABLE_ADDRESSES:
        return [extend_table_ix(existing.key, wallet, wallet, batch)], existing.key
    current = await client.get_slot()
    pending = _pending_create.get(str(wallet))
    slot = pending[1] if pending is not None and current - pending[1] <= _REUSE_SLOT_WITHIN else max(0, current - 1)
    create, table = create_table_ix(wallet, wallet, slot)
    _pending_create[str(wallet)] = (str(table), slot)
    # The memo makes the table findable from the wallet's history by any server instance (`_from_history`).
    memo = Instruction(MEMO_PROGRAM_ID, f"{MEMO_PREFIX}{table}".encode(), [])
    return [create, extend_table_ix(table, wallet, wallet, batch[: MAX_KEYS_PER_SETUP - 3]), memo], table


async def remember_setup(wallet: Pubkey, table: Pubkey) -> None:
    await _remember(wallet, table)


async def stable_keys(client, wallet: Pubkey, registry) -> list[Pubkey]:
    """The wallet's own keys every health-checked margin transaction may name: the margin vault of each allowed token,
    the debt position of each borrowable asset, and the margin's Phoenix trader. Never the margin account itself."""
    from vanna_core.margin_state import allowed_tokens_of, load_protocol_config
    from vanna_core.pdas import debt_position_pda, find_associated_token_address, margin_pda
    from vanna_core.phoenix import trader_pda

    margin = margin_pda(wallet, client.program_id)[0]
    keys: list[Pubkey] = []
    for index in allowed_tokens_of(await load_protocol_config(client)):
        try:
            asset = await registry.by_index(index)
        except ValueError:
            continue
        keys.append(find_associated_token_address(margin, Pubkey.from_string(asset.mint), Pubkey.from_string(asset.token_program)))
    for asset in registry.all(include_unoffered=True):
        if asset.borrow_enabled and asset.has_reserve:
            keys.append(debt_position_pda(margin, Pubkey.from_string(asset.reserve), client.program_id)[0])
    keys.append(trader_pda(margin))
    return list(dict.fromkeys(keys))


async def _missing(client, wallet: Pubkey, registry, table: AddressLookupTableAccount | None, extra) -> list[Pubkey]:
    have = set(table.addresses) if table is not None else set()
    return [k for k in dict.fromkeys([*(await stable_keys(client, wallet, registry)), *(extra or [])]) if k not in have]


async def needs_setup(client, wallet: Pubkey, registry) -> bool:
    """Whether the wallet's table lacks any of its stable keys - a check only: nothing is built or remembered (a
    preview must not leave a pending setup behind). A setup still landing counts as needed."""
    try:
        table = await load_wallet_table(client, wallet)
    except WalletTableNotReady:
        return True
    return bool(await _missing(client, wallet, registry, table, None))


async def setup_envelope_if_needed(client, wallet: Pubkey, registry=None, extra: list[Pubkey] | None = None):
    """The one-time setup (create or extend the wallet's table) when it lacks any of the wallet's stable keys or
    `extra`; None when nothing is missing. Raises `WalletTableNotReady` while a setup is still landing."""
    from vanna_core.assets import AssetRegistry

    if registry is None:
        registry = AssetRegistry(client)
        await registry.load()
    table = await load_wallet_table(client, wallet)
    missing = await _missing(client, wallet, registry, table, extra)
    if not missing:
        return None
    instructions, address = await setup_instructions(client, wallet, table, missing)
    await remember_setup(wallet, address)
    return await client.build_write_envelope(
        instructions, wallet, purpose="wallet_lookup_table",
        summary={
            "action": "wallet_lookup_table_setup",
            "prerequisite": True,
            "lookup_table": str(address),
            "keys_added": min(len(missing), MAX_KEYS_PER_SETUP),
            "next_step": "One-time setup: sign this so your margin transactions fit in one Solana transaction, "
                         "then repeat the same action.",
        },
    )


def clear_wallet_table_cache() -> None:
    _tables.clear()
    _pending_create.clear()
