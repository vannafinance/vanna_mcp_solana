"""Live-fork integration tests for `vanna_protocol_info`."""

from __future__ import annotations

import pytest

from mcp_server.dispatchers.protocol_info import vanna_protocol_info

pytestmark = pytest.mark.asyncio


async def test_config(ctx):
    result = await vanna_protocol_info(action="config")
    assert "error" not in result
    assert result["operating_mode"] in ("Normal", "BorrowPaused", "WithdrawOnly", "Halted")
    assert result["max_assets_per_margin"] == 24
    assert result["next_asset_index"] >= 10  # at least the 5+5 registry assets


async def test_assets_lists_all_ten_registry_assets(ctx):
    result = await vanna_protocol_info(action="assets")
    assert "error" not in result
    symbols = {a["symbol"] for a in result["assets"]}
    assert symbols == {"SOL", "USDC", "USDT", "JitoSOL", "JupSOL", "kSOL", "kUSDC", "kUSDT", "kJitoSOL", "kJupSOL"}
    by_symbol = {a["symbol"]: a for a in result["assets"]}
    # Plain assets: OracleConfig-priced (Scope chain, Pyth fallback), borrow-enabled, real Reserve.
    assert by_symbol["USDC"]["price_source"] == "Oracle"
    assert by_symbol["USDC"]["borrow_enabled"] is True
    assert by_symbol["USDC"]["reserve_status"] == "Active"
    # Kamino receipts: collateral-only, no Vanna Reserve.
    assert by_symbol["kUSDC"]["price_source"] == "KaminoReceipt"
    assert by_symbol["kUSDC"]["borrow_enabled"] is False
    assert by_symbol["kUSDC"]["collateral_enabled"] is True
    assert by_symbol["kUSDC"]["reserve"] is None
    assert by_symbol["kUSDC"]["reserve_status"] is None


async def test_integrations_lists_kamino_and_jupiter_enabled(ctx):
    result = await vanna_protocol_info(action="integrations")
    assert "error" not in result
    adapters = {i["adapter"]: i for i in result["integrations"]}
    assert "KaminoLend" in adapters
    assert "Jupiter" in adapters
    assert adapters["KaminoLend"]["enabled"] is True
    assert adapters["Jupiter"]["enabled"] is True


async def test_unknown_action(ctx):
    result = await vanna_protocol_info(action="bogus")
    assert result["error"] == "invalid_input"
