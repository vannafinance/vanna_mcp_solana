"""Live check of the oracle path: every asset's price/source/age/flags, and the app keeper's response.
Usage: ./.venv/Scripts/python.exe handoff/price_check.py"""
import asyncio
import sys

sys.path.insert(0, "D:/Projects/Vanna_MCP_Solana")

import httpx

from mcp_server.dependencies import VannaContext, _set_context
from mcp_server.dispatchers.oracle import vanna_oracle
from vanna_core.config import VannaSettings, get_settings


async def main() -> None:
    get_settings.cache_clear()
    settings = VannaSettings()
    print("PRICE_REFRESH_SOURCE =", settings.price_refresh_source, "| keeper =", settings.price_keeper_url)
    ctx = VannaContext(settings)
    await ctx.startup()
    _set_context(ctx)
    symbols = ["SOL", "USDC", "USDT", "JitoSOL", "JupSOL", "JupUSD", "NVDAx", "TSLAx", "kSOL", "kUSDC"]
    result = await vanna_oracle(action="prices", assets=symbols)
    for symbol, row in result.items():
        if "error" in row:
            print(f"{symbol:9} ERROR {row}")
            continue
        print(
            f"{symbol:9} ${row['price_usd'][:12]:>12} src={row['source']:<12} age={row['age_seconds']:>6}s "
            f"fresh={int(row['fresh'])} twap={int(row['twap_ok'])} conf={int(row['confidence_ok'])}"
        )
    await ctx.shutdown()

    url = settings.price_keeper_url.rstrip("/") + "/api/prices/refresh"
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.post(url)
        print("keeper", resp.status_code, resp.text[:400])
    except Exception as exc:  # noqa: BLE001
        print("keeper unreachable:", exc)


asyncio.run(main())
