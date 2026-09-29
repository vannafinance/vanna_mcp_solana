from __future__ import annotations

import pytest
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair

from mcp_server.dependencies import VannaContext, _set_context
from vanna_core.config import VannaSettings, get_settings


@pytest.fixture(autouse=True)
def _no_read_cache(monkeypatch):
    """Tests read state before and after their own writes; the read cache would hide the change.
    tests/test_read_cache.py passes an explicit TTL instead."""
    from mcp_server import read_cache

    read_cache.clear_read_cache()
    monkeypatch.setattr(read_cache, "_ttl", lambda: 0.0)


@pytest.fixture
async def ctx():
    get_settings.cache_clear()
    settings = VannaSettings()  # type: ignore[call-arg]
    context = VannaContext(settings)
    await context.startup()
    _set_context(context)
    try:
        yield context
    finally:
        await context.shutdown()
        _set_context(None)


async def _airdrop(rpc_url: str, pubkey, lamports: int) -> None:
    from solana.exceptions import SolanaRpcException

    async with AsyncClient(rpc_url, commitment=Confirmed) as client:
        try:
            sig = (await client.request_airdrop(pubkey, lamports)).value
            await client.confirm_transaction(sig, commitment=Confirmed)
        except SolanaRpcException as exc:
            pytest.skip(f"Fork airdrop cheatcode unavailable/rate-limited: {exc}")


@pytest.fixture(scope="session")
async def funded_fork_pubkey() -> str:
    """A fresh keypair, airdropped 1 SOL on the hosted Surfpool mainnet fork,
    so it exists as a valid fee payer for simulation (Solana requires the fee
    payer account to exist even when sig_verify is disabled). No signing
    capability — use `funded_fork_keypair` when a test needs to actually sign
    and submit (fixture setup only; the MCP itself never signs).
    """
    get_settings.cache_clear()
    settings = VannaSettings()  # type: ignore[call-arg]
    kp = Keypair()
    await _airdrop(settings.solana_rpc_url, kp.pubkey(), 1_000_000_000)
    return str(kp.pubkey())


@pytest.fixture(scope="session")
async def funded_fork_keypair() -> Keypair:
    """Like `funded_fork_pubkey`, but returns the signing keypair — for test
    fixtures that need to actually submit a transaction to create real
    on-chain state (never used by the MCP server itself)."""
    get_settings.cache_clear()
    settings = VannaSettings()  # type: ignore[call-arg]
    kp = Keypair()
    await _airdrop(settings.solana_rpc_url, kp.pubkey(), 3_000_000_000)
    return kp


@pytest.fixture(scope="session")
async def live_margin_position(funded_fork_keypair: Keypair) -> str:
    """A real margin account on the fork with an open position: 1 SOL
    collateral, 1 USDC borrowed. Built once per test session by signing and
    submitting real transactions with a disposable keypair (see
    `tests/_live_helpers.py`) — this is fixture setup, not MCP behavior; the
    MCP itself never signs. Returns the wallet's base58 address.
    """
    from tests._live_helpers import build_with_retry, sign_and_submit

    get_settings.cache_clear()
    settings = VannaSettings()  # type: ignore[call-arg]
    context = VannaContext(settings)
    await context.startup()
    _set_context(context)
    try:
        from mcp_server.dispatchers.account import vanna_account
        from mcp_server.dispatchers.margin_trade import vanna_margin_trade

        authority = str(funded_fork_keypair.pubkey())
        resolved = await vanna_account(action="resolve", authority=authority)
        if not resolved["exists"]:
            envelope = await vanna_account(action="create", authority=authority)
            assert envelope["simulation_success"], envelope
            await sign_and_submit(settings.solana_rpc_url, envelope, funded_fork_keypair)

        state = await vanna_account(action="get", authority=authority)
        if "SOL" not in state["collateral_assets"]:
            envelope = await vanna_margin_trade(action="deposit", authority=authority, asset="SOL", amount="1")
            assert envelope["simulation_success"], envelope.get("reason")
            await sign_and_submit(settings.solana_rpc_url, envelope, funded_fork_keypair)

        state = await vanna_account(action="get", authority=authority)
        if "USDC" not in state["debt_assets"]:
            # The borrow path is health-checked (needs a fresh USDC price) —
            # retry on the known stale-price symptom (see _live_helpers.py).
            envelope = await build_with_retry(
                lambda: vanna_margin_trade(action="borrow", authority=authority, asset="USDC", amount="1")
            )
            assert envelope["simulation_success"], envelope.get("reason")
            await sign_and_submit(settings.solana_rpc_url, envelope, funded_fork_keypair)

        return authority
    finally:
        await context.shutdown()
        _set_context(None)
