"""Time basic JSON-RPC calls against the fork, to report whether it is responsive.

Usage: python handoff/rpc_probe.py [rpc_url]   (defaults to the configured SOLANA_RPC_URL)
"""

import asyncio
import sys
import time

import httpx

sys.path.insert(0, "D:/Projects/Vanna_MCP_Solana")
from vanna_core.config import get_settings

CALLS = [
    ("getHealth", []),
    ("getVersion", []),
    ("getSlot", []),
    ("getAccountInfo", ["SysvarC1ock11111111111111111111111111111111", {"encoding": "base64"}]),
    ("getAccountInfo", ["H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC", {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]),
]


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else get_settings().solana_rpc_url
    print("RPC", url)
    async with httpx.AsyncClient(timeout=30) as http:
        for method, params in CALLS:
            start = time.perf_counter()
            try:
                resp = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                print(f"{method:15} HTTP {resp.status_code} {time.perf_counter() - start:6.2f}s {resp.text[:160].strip()}")
            except Exception as exc:  # noqa: BLE001
                print(f"{method:15} FAILED {time.perf_counter() - start:6.2f}s {type(exc).__name__}")


asyncio.run(main())
