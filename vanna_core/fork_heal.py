"""Self-heal for swaps that fail on the Surfpool fork because a DEX pool's cloned state has drifted from mainnet.

Ported from vanna-credit-backend `lib/solana/fork-heal.ts` (`healStaleDexPools`) and
`lib/copilot/workflow/swap-heal.ts` (`STALE_FORK_SWAP`). The fork lazy-clones a concentrated-liquidity pool once and
then freezes it, while Jupiter quotes against live mainnet. A large swap then crosses bins or ticks whose fork state is
out of date and fails simulation (Jupiter 6001 slippage, Anchor 3005, a stale bin array). Resetting those accounts with
Surfpool's `surfnet_resetAccount` cheatcode re-clones them fresh from mainnet, so a rebuilt swap simulates again.

The Copilot does this around its own MCP calls; an outside MCP client talks to this server directly, so the swap
builders heal here. Fork only: `surfnet_resetAccount` does not exist anywhere else, and the callers check
`settings.is_fork` first. Nothing here signs or sends a user transaction; it only reads the account list of an
unsigned one.
"""

from __future__ import annotations

import base64
import logging
import re

import httpx
from solders.address_lookup_table_account import AddressLookupTable
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from vanna_core.client import SolanaClient
from vanna_core.envelope import WriteEnvelope

logger = logging.getLogger(__name__)

# Concentrated-liquidity AMMs Jupiter routes through on the fork; their clones drift the longer the fork runs.
STALE_PRONE_DEX_PROGRAMS = frozenset({
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
})
TOKEN_PROGRAMS = frozenset({
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
})

# Swap simulation failures that mean "the fork's pool state is stale", not "this swap is wrong".
STALE_FORK_SWAP = re.compile(
    "|".join((
        r"AccountNotEnoughKeys|\b3005\b|0x1771|\b6001\b|SlippageToleranceExceeded|0x1788|\b6024\b|0x1787|\b6023\b|0xbbd",
        r"bin_array|tick_?array|stale[_ ]pool|stale[_ ]bin|invalid index",
        r"(?:" + "|".join(sorted(STALE_PRONE_DEX_PROGRAMS)) + r") failed",
    )),
    re.IGNORECASE,
)

# How many times a swap is rebuilt after a heal (same as the Copilot's MAX_SWAP_HEALS).
MAX_SWAP_HEALS = 2


def looks_stale(envelope: WriteEnvelope) -> bool:
    if envelope.simulation_success or not envelope.transactions:
        return False
    sim = envelope.transactions[0].simulation
    text = "\n".join([envelope.reason or "", sim.err or "", *sim.logs])
    return bool(STALE_FORK_SWAP.search(text))


def unsigned_transaction(envelope: WriteEnvelope) -> VersionedTransaction | None:
    b64 = envelope.transactions[0].unsigned_transaction if envelope.transactions else None
    if not b64:
        return None
    try:
        return VersionedTransaction.from_bytes(base64.b64decode(b64))
    except Exception:  # noqa: BLE001
        return None


async def heal_stale_dex_pools(client: SolanaClient, tx: VersionedTransaction) -> int:
    """Reset every account `tx` touches that a stale-prone DEX owns, those pools' token vaults, and any lookup table
    shorter than the indexes `tx` references. Returns how many accounts were reset (0: nothing to fix, so the caller
    must not retry and mask a different failure)."""
    try:
        message = tx.message
        keys = [str(k) for k in message.account_keys]
        stale_tables: list[str] = []
        for lookup in message.address_table_lookups:
            # A route's lookup table can be stale too: mainnet keeps extending it while the fork's clone is frozen.
            indexes = [*lookup.writable_indexes, *lookup.readonly_indexes]
            data = await client.fetch_raw_account_or_none(lookup.account_key)
            addresses = list(AddressLookupTable.deserialize(bytes(data)).addresses) if data else []
            if not addresses or len(addresses) <= max(indexes, default=-1):
                stale_tables.append(str(lookup.account_key))
                continue
            keys += [str(addresses[i]) for i in indexes]
        infos = await client.get_multiple_accounts_raw([Pubkey.from_string(k) for k in dict.fromkeys(keys)])
        dex_owned = {k for k, info in infos.items() if info and info[0] in STALE_PRONE_DEX_PROGRAMS}
        # A pool's vaults belong to a token program but name the pool as their owner field (bytes 32..64): reset them
        # with the pool so its bins and its vault balances stay consistent.
        vaults = {
            k for k, info in infos.items()
            if info and info[0] in TOKEN_PROGRAMS and len(info[1]) >= 64 and str(Pubkey.from_bytes(info[1][32:64])) in dex_owned
        }
        stale = sorted(dex_owned | vaults | set(stale_tables))
        if not stale:
            return 0
        async with httpx.AsyncClient(timeout=30.0) as http:
            for address in stale:
                try:
                    await http.post(client.settings.solana_rpc_url,
                                    json={"jsonrpc": "2.0", "id": 1, "method": "surfnet_resetAccount", "params": [address]})
                except httpx.HTTPError:
                    pass
        logger.info("fork heal: reset %d stale DEX account(s)", len(stale))
        return len(stale)
    except Exception as exc:  # noqa: BLE001  (a heal that cannot run just means no retry)
        logger.warning("fork heal failed: %s", exc)
        return 0
