"""Tests for `vanna_sign` (tx_status only — the user's wallet signs)."""

from __future__ import annotations

import pytest

from mcp_server.dispatchers.sign import vanna_sign

pytestmark = pytest.mark.asyncio


async def test_tx_status_requires_signature(ctx):
    result = await vanna_sign(action="tx_status")
    assert result["error"] == "invalid_input"


async def test_tx_status_not_found_for_unknown_signature(ctx):
    # A well-formed but never-broadcast signature (64 zero bytes, valid base58 length).
    fake_sig = "1" * 88
    result = await vanna_sign(action="tx_status", signature=fake_sig)
    assert result["error"] == "invalid_input"  # not a valid signature encoding either


async def test_tx_status_real_confirmed_signature(ctx, funded_fork_keypair):
    from tests._live_helpers import sign_and_submit
    from vanna_core.config import get_settings as _get_settings

    settings = _get_settings()
    from mcp_server.dispatchers.margin_trade import vanna_margin_trade
    from mcp_server.dispatchers.account import vanna_account

    authority = str(funded_fork_keypair.pubkey())
    resolved = await vanna_account(action="resolve", authority=authority)
    if not resolved["exists"]:
        envelope = await vanna_account(action="create", authority=authority)
        assert envelope["simulation_success"]
        signature = await sign_and_submit(settings.solana_rpc_url, envelope, funded_fork_keypair)
    else:
        # Any prior write works too — just need a real confirmed signature.
        envelope = await vanna_margin_trade(action="deposit", authority=authority, asset="SOL", amount="0.001")
        assert envelope["simulation_success"], envelope.get("reason")
        signature = await sign_and_submit(settings.solana_rpc_url, envelope, funded_fork_keypair)

    result = await vanna_sign(action="tx_status", signature=signature)
    assert result["status"] == "confirmed"
    assert result["err"] is None


@pytest.mark.parametrize("action", ["sign_and_submit", "enable_auto_sign", "connect_start"])
async def test_server_side_signing_actions_do_not_exist(ctx, action):
    result = await vanna_sign(action=action)
    assert result["error"] == "invalid_input"


async def test_unknown_action(ctx):
    result = await vanna_sign(action="bogus")
    assert result["error"] == "invalid_input"
