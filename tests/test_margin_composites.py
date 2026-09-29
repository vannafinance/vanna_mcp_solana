"""Live-fork integration tests for the two `vanna_margin_trade` composite
actions: `deposit_and_borrow` (atomic deposit + borrow in one transaction)
and `close_position` (repeatedly-callable full unwind). Both need a
dedicated, disposable margin account — never `live_margin_position`, which
is a *session-scoped, shared* fixture other tests also read from; draining
or closing it here would break every other test in the session.
"""

from __future__ import annotations

import asyncio

import pytest
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair

from mcp_server.dispatchers.account import vanna_account
from mcp_server.dispatchers.margin_trade import vanna_margin_trade
from mcp_server.dispatchers.swap import vanna_swap
from tests._live_helpers import build_with_retry, is_stale_fork_pool_error, sign_and_submit

pytestmark = pytest.mark.asyncio


async def _fresh_funded_keypair(rpc_url: str, lamports: int = 2_000_000_000) -> Keypair:
    kp = Keypair()
    async with AsyncClient(rpc_url, commitment=Confirmed) as client:
        try:
            sig = (await client.request_airdrop(kp.pubkey(), lamports)).value
            await client.confirm_transaction(sig, commitment=Confirmed)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Fork airdrop cheatcode unavailable/rate-limited: {exc}")
    return kp


async def test_deposit_and_borrow_missing_args(ctx):
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_trade(action="deposit_and_borrow", authority=fresh)
    assert result["error"] == "invalid_input"


async def test_close_position_on_nonexistent_margin_reports_done(ctx):
    # No margin account ever existed for this wallet -> already-terminal
    # state, not an error (mirrors "closed by a previous call").
    fresh = str(Keypair().pubkey())
    result = await vanna_margin_trade(action="close_position", authority=fresh)
    assert result["summary"]["next_step"] == "done"
    assert result["simulation_success"] is True


async def test_deposit_and_borrow_atomic_live(ctx):
    settings = ctx.settings
    kp = await _fresh_funded_keypair(settings.solana_rpc_url)
    authority = str(kp.pubkey())

    created = await vanna_account(action="create", authority=authority)
    assert created["simulation_success"], created.get("reason")
    await sign_and_submit(settings.solana_rpc_url, created, kp)

    envelope = await build_with_retry(
        lambda: vanna_margin_trade(
            action="deposit_and_borrow", authority=authority,
            deposit_asset="SOL", deposit_amount="1", borrow_asset="USDC", borrow_amount="1",
        )
    )
    assert envelope["simulation_success"], envelope.get("reason")
    # One atomic transaction, not two.
    assert len(envelope["transactions"]) == 1
    await sign_and_submit(settings.solana_rpc_url, envelope, kp)

    state = await vanna_account(action="get", authority=authority)
    assert "SOL" in state["collateral_assets"]
    assert "USDC" in state["debt_assets"]

    health = await vanna_account(action="health", authority=authority)
    assert health["is_healthy"] is True


async def test_close_position_full_unwind_live(ctx):
    settings = ctx.settings
    kp = await _fresh_funded_keypair(settings.solana_rpc_url, lamports=3_000_000_000)
    authority = str(kp.pubkey())

    created = await vanna_account(action="create", authority=authority)
    assert created["simulation_success"], created.get("reason")
    await sign_and_submit(settings.solana_rpc_url, created, kp)

    db = await build_with_retry(
        lambda: vanna_margin_trade(
            action="deposit_and_borrow", authority=authority,
            deposit_asset="SOL", deposit_amount="1", borrow_asset="USDC", borrow_amount="1",
        )
    )
    assert db["simulation_success"], db.get("reason")
    await sign_and_submit(settings.solana_rpc_url, db, kp)

    # `repay_all` needs the margin vault to cover debt + accrued-interest
    # dust (borrow.rs's own rounding-up), which a vault holding only the
    # exact borrowed principal never quite does — get a little wallet USDC
    # ready for the `needs_wallet_topup` step close_position will report.
    wallet_swap = None
    for _ in range(6):
        wallet_swap = await vanna_swap(
            action="wallet_swap", authority=authority, from_asset="SOL", to_asset="USDC", amount="0.3"
        )
        if wallet_swap.get("simulation_success"):
            break
        if not is_stale_fork_pool_error(wallet_swap.get("reason")):
            break
        await asyncio.sleep(3)
    if not wallet_swap.get("simulation_success"):
        pytest.skip(f"Jupiter fork-pool wallet_swap unavailable this run: {wallet_swap.get('reason')}")
    await sign_and_submit(settings.solana_rpc_url, wallet_swap, kp)

    seen_steps = []
    for _ in range(15):
        envelope = await build_with_retry(lambda: vanna_margin_trade(action="close_position", authority=authority))
        assert "error" not in envelope or envelope.get("error") is None, envelope
        next_step = envelope["summary"]["next_step"]
        seen_steps.append(next_step)

        if next_step == "done":
            break
        if next_step == "needs_wallet_topup":
            for shortfall in envelope["summary"]["shortfalls"]:
                topup = await build_with_retry(
                    lambda shortfall=shortfall: vanna_margin_trade(
                        action="repay_from_wallet", authority=authority, payer=authority,
                        asset=shortfall["symbol"], repay_all=True,
                    )
                )
                assert topup["simulation_success"], topup.get("reason")
                await sign_and_submit(settings.solana_rpc_url, topup, kp)
            continue

        assert envelope["simulation_success"], envelope.get("reason")
        await sign_and_submit(settings.solana_rpc_url, envelope, kp)
    else:
        pytest.fail(f"close_position did not reach 'done' within 15 iterations, steps seen: {seen_steps}")

    assert seen_steps[-1] == "done"
    resolved = await vanna_account(action="resolve", authority=authority)
    assert resolved["exists"] is False


async def test_close_position_called_twice_after_done_stays_done(ctx):
    """Idempotency: once a position is fully closed, repeated calls keep
    reporting 'done' rather than erroring."""
    settings = ctx.settings
    kp = await _fresh_funded_keypair(settings.solana_rpc_url)
    authority = str(kp.pubkey())

    created = await vanna_account(action="create", authority=authority)
    assert created["simulation_success"], created.get("reason")
    await sign_and_submit(settings.solana_rpc_url, created, kp)

    envelope = await build_with_retry(lambda: vanna_margin_trade(action="close_position", authority=authority))
    assert envelope["summary"]["next_step"] == "close_margin"
    await sign_and_submit(settings.solana_rpc_url, envelope, kp)

    for _ in range(2):
        envelope = await vanna_margin_trade(action="close_position", authority=authority)
        assert envelope["summary"]["next_step"] == "done"
        assert envelope["simulation_success"] is True


async def test_borrow_and_supply_rejected_for_collateral_only_assets(ctx):
    """JitoSOL/JupSOL are collateral + swap + farm only (product policy) even
    though their on-chain reserves are live."""
    fresh = str(Keypair().pubkey())
    for asset in ("JitoSOL", "JupSOL"):
        result = await vanna_margin_trade(action="borrow", authority=fresh, asset=asset, amount="1")
        assert result["error"] == "asset_not_offered"
        result = await vanna_margin_trade(
            action="deposit_and_borrow", authority=fresh,
            deposit_asset="SOL", deposit_amount="1", borrow_asset=asset, borrow_amount="1",
        )
        assert result["error"] == "asset_not_offered"
        result = await vanna_margin_trade(action="open_debt", authority=fresh, asset=asset)
        assert result["error"] == "asset_not_offered"

    from mcp_server.dispatchers.earn_write import vanna_earn_write

    result = await vanna_earn_write(action="supply", owner=fresh, asset="JitoSOL", amount="1")
    assert result["error"] == "asset_not_offered"
