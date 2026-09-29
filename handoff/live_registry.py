import asyncio
import sys

sys.path.insert(0, "D:/Projects/Vanna_MCP_Solana")
from vanna_core.assets import AssetRegistry
from vanna_core.client import SolanaClient
from vanna_core.config import VannaSettings, get_settings


async def main():
    get_settings.cache_clear()
    client = SolanaClient(VannaSettings())
    reg = AssetRegistry(client)
    await reg.load()
    for a in reg.all():
        o = a.oracle
        print(
            f"{a.symbol:9} idx={a.asset_index:>3} dec={a.decimals} col={int(a.collateral_enabled)} bor={int(a.borrow_enabled)} "
            f"tp={a.token_program[:4]} reserve={'yes' if a.has_reserve else 'no '} "
            f"scope={list(c for c in o.scope_chain if c != 65535)} twap={list(c for c in o.scope_twap_chain if c != 65535)} "
            f"pyth={o.pyth_price[:5] or '-'} factor={o.pyth_factor[:5] or '-'} klend={o.klend_reserve[:5] or '-'} "
            f"age={o.max_age_secs} twapbps={o.max_twap_divergence_bps} conf={o.max_confidence_bps} maxcol={a.max_collateral_per_margin}"
        )
    await client.close()


asyncio.run(main())
