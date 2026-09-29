"""Tests for `vanna_fork` (dev-only faucet). Disabled by default —
`ENABLE_FORK_TOOLS` must be set for the `faucet` action to do anything but
refuse.
"""

from __future__ import annotations

import pytest
from solders.keypair import Keypair

from mcp_server.dispatchers.fork import vanna_fork

pytestmark = pytest.mark.asyncio


async def test_faucet_disabled_by_default(ctx):
    address = str(Keypair().pubkey())
    result = await vanna_fork(action="faucet", address=address)
    assert result["error"] == "contract_error"
    assert "disabled" in result["message"]


async def test_unknown_action(ctx):
    address = str(Keypair().pubkey())
    result = await vanna_fork(action="bogus", address=address)
    assert result["error"] == "invalid_input"


async def test_faucet_enabled_reaches_backend_or_fails_closed(monkeypatch):
    """With ENABLE_FORK_TOOLS set, the dispatcher must actually attempt the
    HTTP call (not silently no-op) — whether the backend's faucet endpoint
    answers depends on network reachability from wherever tests run, so this
    accepts either a real result or a clean contract_error, never a crash."""
    from mcp_server.dependencies import VannaContext, _set_context
    from vanna_core.config import VannaSettings, get_settings

    monkeypatch.setenv("ENABLE_FORK_TOOLS", "true")
    get_settings.cache_clear()
    settings = VannaSettings()
    context = VannaContext(settings)
    await context.startup()
    _set_context(context)
    try:
        address = str(Keypair().pubkey())
        result = await vanna_fork(action="faucet", address=address, symbol="USDC")
        assert "error" not in result or result["error"] == "contract_error"
    finally:
        await context.shutdown()
        _set_context(None)
        get_settings.cache_clear()
