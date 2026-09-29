"""Live-fork integration tests for `vanna_oracle`."""

from __future__ import annotations

import pytest

from mcp_server.dispatchers.oracle import vanna_oracle

pytestmark = pytest.mark.asyncio


async def test_price_sol(ctx):
    result = await vanna_oracle(action="price", asset="SOL")
    assert "error" not in result
    assert result["symbol"] == "SOL"
    assert float(result["price_usd"]) > 0
    assert result["source"] in ("scope", "pyth")  # Scope while fresh, else the Pyth fallback
    assert result["oracle"]["scope"] is True and result["oracle"]["pyth_fallback"] is True
    for flag in ("fresh", "twap_ok", "confidence_ok"):
        assert flag in result


async def test_price_accepts_wsol_alias(ctx):
    result = await vanna_oracle(action="price", asset="wSOL")
    assert result["symbol"] == "SOL"


async def test_price_jupsol_is_a_pyth_pair_fallback_priced_asset(ctx):
    # JupSOL has no USD feed: Scope chain [JupSOL rate x SOL/USD], Pyth fallback = JUPSOL/SOL x SOL/USD.
    result = await vanna_oracle(action="price", asset="JupSOL")
    assert "error" not in result
    assert result["oracle"]["scope"] is True and result["oracle"]["pyth_factor"] is True
    assert result["source"] in ("scope", "pyth_x_pyth")
    assert float(result["price_usd"]) > 0


async def test_price_jitosol_is_scope_only(ctx):
    result = await vanna_oracle(action="price", asset="JitoSOL")
    assert "error" not in result
    assert result["oracle"]["scope"] is True and result["oracle"]["pyth_fallback"] is False
    assert result["source"] == "scope"


async def test_price_receipt_uses_kamino_exchange_rate(ctx):
    result = await vanna_oracle(action="price", asset="kUSDC")
    assert "error" not in result
    assert result["source"] == "kamino_receipt"
    assert result["underlying_symbol"] == "USDC"
    assert float(result["price_usd"]) > 0


async def test_price_unknown_asset(ctx):
    result = await vanna_oracle(action="price", asset="DOGE")
    assert result["error"] == "invalid_input"


async def test_price_missing_asset_arg(ctx):
    result = await vanna_oracle(action="price")
    assert result["error"] == "invalid_input"


async def test_prices_batch_all_five(ctx):
    result = await vanna_oracle(action="prices", assets=["SOL", "USDC", "USDT", "JitoSOL", "JupSOL"])
    assert "error" not in result
    assert set(result.keys()) == {"SOL", "USDC", "USDT", "JitoSOL", "JupSOL"}
    for symbol, price in result.items():
        assert "error" not in price, f"{symbol}: {price}"
        assert float(price["price_usd"]) > 0


async def test_prices_missing_assets_arg(ctx):
    result = await vanna_oracle(action="prices")
    assert result["error"] == "invalid_input"


async def test_unknown_action(ctx):
    result = await vanna_oracle(action="bogus", asset="SOL")
    assert result["error"] == "invalid_input"
