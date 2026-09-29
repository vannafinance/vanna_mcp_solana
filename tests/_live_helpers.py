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
    disposable local keypair, purely to create fixture state for read tests.

    Sends through raw JSON-RPC (not solana-py's `send_raw_transaction`, which panics while parsing the
    fork's preflight-failure body), so a real send error surfaces with its logs instead of a parser crash.
    Transient send errors are retried; the wait-for-confirmation is a status poll."""
    import httpx

    tx_b64 = envelope["transactions"][0]["unsignedTransaction"]
    unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    signed = VersionedTransaction(unsigned.message, [keypair, *(extra_signers or [])])
    payload = base64.b64encode(bytes(signed)).decode()

    async def rpc(http: httpx.AsyncClient, method: str, params: list) -> dict:
        resp = await http.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        return resp.json()

    async with httpx.AsyncClient(timeout=60.0) as http:
        signature = None
        for attempt in range(4):
            body = await rpc(http, "sendTransaction", [payload, {"encoding": "base64", "preflightCommitment": "confirmed"}])
            if "result" in body:
                signature = body["result"]
                break
            message = str(body.get("error"))
            transient = any(m in message.lower() for m in ("blockhash not found", "internal error", "timed out"))
            if not transient or attempt == 3:
                raise RuntimeError(f"sendTransaction failed: {message}")
            await asyncio.sleep(1.5)
        for _ in range(60):
            body = await rpc(http, "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            status = (body.get("result", {}).get("value") or [None])[0]
            if status is not None:
                if status.get("err") is not None:
                    raise RuntimeError(f"transaction {signature} failed on-chain: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return signature
            await asyncio.sleep(1.0)
    raise RuntimeError(f"transaction {signature} was not confirmed within 60s")


async def fund_token(rpc_url: str, owner: str, mint: str, amount_raw: int, token_program: str) -> None:
    """Fork-only: set `owner`'s balance of `mint` via the `surfnet_setTokenAccount` cheatcode (works for
    classic SPL and Token-2022). Test fixture setup only - the MCP itself never calls this."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(
            rpc_url,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "surfnet_setTokenAccount",
                "params": [owner, mint, {"amount": amount_raw}, token_program],
            },
        )
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"surfnet_setTokenAccount failed: {body['error']}")
