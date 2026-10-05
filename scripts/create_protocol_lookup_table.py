"""Create, fill and freeze the protocol address lookup table (see vanna_core/protocol_alt.py).

    python scripts/create_protocol_lookup_table.py                   # fork: a throwaway payer is airdropped
    python scripts/create_protocol_lookup_table.py --payer key.json  # any cluster: pay from a keypair file

Prints the table address; set it as PROTOCOL_LOOKUP_TABLES on the MCP. The table is frozen at the end, so
its contents can never change and no authority key has to be kept. When the program registers a new asset,
create a new table and list both (comma-separated). A fork reset deletes the table; builds then skip it
until a new one is created.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from solana.rpc.async_api import AsyncClient  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solana.rpc.commitment import Confirmed  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from vanna_core.assets import AssetRegistry  # noqa: E402
from vanna_core.client import SolanaClient  # noqa: E402
from vanna_core.config import VannaSettings  # noqa: E402
from vanna_core.protocol_alt import (  # noqa: E402
    create_table_ix,
    extend_table_ix,
    freeze_table_ix,
    protocol_lookup_addresses,
)

EXTEND_CHUNK = 20  # addresses per extend transaction (keeps each well under the packet limit)


async def send(rpc: AsyncClient, payer: Keypair, ixs) -> str:
    blockhash = (await rpc.get_latest_blockhash()).value.blockhash
    tx = VersionedTransaction(MessageV0.try_compile(payer.pubkey(), ixs, [], blockhash), [payer])
    # Raw JSON-RPC so an error body is shown as-is (some fork error replies don't parse in solana-py).
    async with httpx.AsyncClient(timeout=60.0) as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "preflightCommitment": "confirmed"}]}
        reply = (await http.post(str(rpc._provider.endpoint_uri), json=body)).json()
    if "error" in reply:
        raise SystemExit(f"sendTransaction failed: {json.dumps(reply['error'])[:1500]}")
    sig = Signature.from_string(reply["result"])
    await rpc.confirm_transaction(sig, commitment=Confirmed)
    return str(sig)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payer", help="keypair JSON file (array of 64 bytes); default: airdropped throwaway on a fork")
    args = parser.parse_args()

    settings = VannaSettings()  # type: ignore[call-arg]
    client = SolanaClient(settings)
    registry = AssetRegistry(client)
    await registry.load()
    addresses = await protocol_lookup_addresses(client.program_id, registry, settings)
    print(f"{len(addresses)} protocol addresses")

    async with AsyncClient(settings.solana_rpc_url, commitment=Confirmed) as rpc:
        if args.payer:
            payer = Keypair.from_bytes(bytes(json.loads(Path(args.payer).read_text())))
        else:
            if "fork" not in settings.solana_cluster_label:
                raise SystemExit("Not a fork: pass --payer with a funded keypair.")
            payer = Keypair()
            sig = (await rpc.request_airdrop(payer.pubkey(), 1_000_000_000)).value
            await rpc.confirm_transaction(sig, commitment=Confirmed)

        slot = (await rpc.get_slot(commitment=Confirmed)).value
        create, table = create_table_ix(payer.pubkey(), payer.pubkey(), slot - 1)
        print("create", await send(rpc, payer, [create]))
        for i in range(0, len(addresses), EXTEND_CHUNK):
            chunk = addresses[i : i + EXTEND_CHUNK]
            print(f"extend {i}-{i + len(chunk) - 1}", await send(rpc, payer, [extend_table_ix(table, payer.pubkey(), payer.pubkey(), chunk)]))
        print("freeze", await send(rpc, payer, [freeze_table_ix(table, payer.pubkey())]))
    await client.close()
    print(f"\nPROTOCOL_LOOKUP_TABLES={table}")


if __name__ == "__main__":
    asyncio.run(main())
