"""Check whether the Vanna program and its core PDAs exist on the configured fork.

Usage: python handoff/program_probe.py [rpc_url]
"""

import asyncio
import base64
import sys

import httpx

sys.path.insert(0, "D:/Projects/Vanna_MCP_Solana")
from solders.pubkey import Pubkey

from vanna_core.assets import known_mints
from vanna_core.config import get_settings
from vanna_core.pdas import asset_config_pda, protocol_config_pda, reserve_pda

BPF_UPGRADEABLE_LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"
KLEND = "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"


async def account(http: httpx.AsyncClient, url: str, address: str) -> dict | None:
    resp = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                      "params": [address, {"encoding": "base64"}]})
    body = resp.json()
    if "error" in body:
        return {"error": body["error"]}
    return body["result"]["value"]


def describe(value: dict | None) -> str:
    if value is None:
        return "MISSING"
    if "error" in value:
        return f"RPC error {value['error']}"
    data = base64.b64decode(value["data"][0])
    return f"owner={value['owner']} len={len(data)} executable={value.get('executable')}"


async def main() -> None:
    settings = get_settings()
    url = sys.argv[1] if len(sys.argv) > 1 else settings.solana_rpc_url
    program_id = Pubkey.from_string(settings.vanna_program_id)
    print("RPC", url, "program", program_id)
    async with httpx.AsyncClient(timeout=60) as http:
        program = await account(http, url, str(program_id))
        print(f"{'program':18} {describe(program)}")
        if program and "error" not in program and program["owner"] == BPF_UPGRADEABLE_LOADER:
            programdata = Pubkey.find_program_address([bytes(program_id)], Pubkey.from_string(BPF_UPGRADEABLE_LOADER))[0]
            print(f"{'programdata':18} {describe(await account(http, url, str(programdata)))}")
        print(f"{'klend (control)':18} {describe(await account(http, url, KLEND))}")
        print(f"{'ProtocolConfig':18} {describe(await account(http, url, str(protocol_config_pda(program_id)[0])))}")
        for mint in known_mints():
            m = Pubkey.from_string(mint)
            cfg = describe(await account(http, url, str(asset_config_pda(m, program_id)[0])))
            res = describe(await account(http, url, str(reserve_pda(m, program_id)[0])))
            print(f"{mint[:8]:18} AssetConfig {cfg} | Reserve {res}")


asyncio.run(main())
