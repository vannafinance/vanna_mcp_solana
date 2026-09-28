from __future__ import annotations

import pytest
from solders.keypair import Keypair

from mcp_server.dependencies import VannaContext, _set_context
from vanna_core.config import VannaSettings, get_settings


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


@pytest.fixture(scope="session")
async def funded_devnet_pubkey() -> str:
    """A fresh devnet keypair, airdropped 0.05 SOL so it exists as a valid fee
    payer for simulation (Solana requires the fee payer account to exist even
    when sig_verify is disabled).

    The public devnet RPC's airdrop faucet is heavily rate-limited (see
    Backend-Solana's .env.example) and frequently 429s. Skip rather than fail
    the suite when that happens — it's an external-service availability issue,
    not a defect in this code.
    """
    from solana.exceptions import SolanaRpcException
    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment import Confirmed

    kp = Keypair()
    async with AsyncClient("https://api.devnet.solana.com", commitment=Confirmed) as client:
        try:
            sig = (await client.request_airdrop(kp.pubkey(), 50_000_000)).value
            await client.confirm_transaction(sig, commitment=Confirmed)
        except SolanaRpcException as exc:
            pytest.skip(f"Devnet airdrop faucet unavailable/rate-limited: {exc}")
    return str(kp.pubkey())
