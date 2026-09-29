"""Test-only helpers for signing and submitting a write envelope on the live
fork. NEVER used by the MCP server itself — the server only ever builds and
simulates (TOOLS_CATALOGUE.md's "the MCP never signs or submits" rule applies
to production dispatcher code, not to test fixtures that need real on-chain
state to test reads against, same pattern as the project's own
`scripts/e2e-fork.ts` / `scripts/e2e-exec.ts`).
"""

from __future__ import annotations

import base64

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction


import asyncio
import re
from typing import Awaitable, Callable

# Jupiter's own SlippageToleranceExceeded (0x1771/6001) or a cloned CLMM/DLMM
# pool's stale-bin symptom (0x1788/6024 among others) — a known Surfpool
# mainnet-fork artifact (cloned DEX pool state drifts from what Jupiter's
# live-mainnet router quotes against), not a defect in the instruction
# construction. `vanna-credit-backend`'s own `isStaleForkPoolError` /
# `healStaleDexPools` exists precisely for this; that heal loop isn't built
# here yet (BUILD_PLAN.md), so tests retry with a fresh quote instead.
_STALE_FORK_POOL_PATTERN = re.compile(
    r"0x1771|custom program error:\s*6001|SlippageToleranceExceeded|0x1788|0xbbd|custom program error:\s*3005",
    re.IGNORECASE,
)


def is_stale_fork_pool_error(reason: str | None) -> bool:
    return bool(reason and _STALE_FORK_POOL_PATTERN.search(reason))


# The on-chain program's own StalePrice error (6027). Writes refresh prices
# first (DECISIONS.md D4), but a price can still go stale between the refresh
# and the simulation. Fail-closed here is correct behavior, not a bug — tests
# retry with a short wait instead of asserting success on the first attempt.
_STALE_PRICE_PATTERN = re.compile(r"StalePrice|0x178b|custom program error:\s*6027", re.IGNORECASE)


def is_stale_price_error(reason: str | None) -> bool:
    return bool(reason and _STALE_PRICE_PATTERN.search(reason))


async def build_with_retry(
    build_fn: Callable[[], Awaitable[dict]], *, attempts: int = 6, delay_seconds: float = 5.0
) -> dict:
    """Call `build_fn` (an envelope-returning coroutine factory) until it
    simulates successfully or `attempts` is exhausted. Only retries on the
    recognized stale-price symptom — any other failure returns immediately
    so a real bug still fails the test loudly on the first try."""
    envelope = None
    for attempt in range(attempts):
        envelope = await build_fn()
        if envelope.get("simulation_success"):
            return envelope
        if not is_stale_price_error(envelope.get("reason")):
            return envelope
        if attempt < attempts - 1:
            await asyncio.sleep(delay_seconds)
    return envelope


async def sign_and_submit(rpc_url: str, envelope: dict, keypair: Keypair, extra_signers: list[Keypair] | None = None) -> str:
    """Sign and submit a schema-v1 write envelope's single transaction with a
    disposable local keypair, purely to create fixture state for read tests."""
    tx_b64 = envelope["transactions"][0]["unsignedTransaction"]
    raw = base64.b64decode(tx_b64)
    unsigned = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(unsigned.message, [keypair, *(extra_signers or [])])
    async with AsyncClient(rpc_url, commitment=Confirmed) as client:
        signature = (await client.send_raw_transaction(bytes(signed))).value
        await client.confirm_transaction(signature, commitment=Confirmed)
    return str(signature)
