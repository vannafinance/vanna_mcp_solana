"""Jupiter v6 HTTP client — quotes, swap-instructions (for `margin_swap`), and
full swap transactions (for `wallet_swap`). Verified against
`vanna-credit-backend/lib/solana/server/jupiter-api.ts` and `exec.ts`'s
`executeMarginSwap` (2026-09-28): same base-URL toggle, same
`wrapAndUnwrapSol=false`/`useSharedAccounts=true` margin-swap request shape,
same fork-safe DEX allowlist.
"""

from __future__ import annotations

import base64

import httpx
from solders.address_lookup_table_account import AddressLookupTable, AddressLookupTableAccount
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from vanna_core.client import SolanaClient
from vanna_core.config import VannaSettings
from vanna_core.types import ContractCallError

_KEYLESS_BASE = "https://lite-api.jup.ag"
_KEYED_BASE = "https://api.jup.ag"

# sha256("global:route")[:8] / sha256("global:shared_accounts_route")[:8] —
# the only two selectors the on-chain Jupiter adapter allows (adapters/jupiter.rs).
ROUTE_SELECTOR = bytes([229, 23, 203, 151, 122, 227, 173, 42])
SHARED_ACCOUNTS_ROUTE_SELECTOR = bytes([193, 32, 155, 51, 65, 214, 156, 129])
ALLOWED_SELECTORS = {ROUTE_SELECTOR, SHARED_ACCOUNTS_ROUTE_SELECTOR}


def _safe_json(resp: httpx.Response, what: str) -> dict:
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — a non-JSON response (rate-limit page, etc.)
        raise ContractCallError(f"{what}: HTTP {resp.status_code}, non-JSON response ({exc})") from exc


class JupiterClient:
    def __init__(self, settings: VannaSettings) -> None:
        self._settings = settings

    @property
    def _base_url(self) -> str:
        return _KEYED_BASE if self._settings.jupiter_api_key else _KEYLESS_BASE

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._settings.jupiter_api_key} if self._settings.jupiter_api_key else {}

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int = 100,
        *,
        max_accounts: int | None = None,
        restrict_intermediate_tokens: bool = False,
    ) -> dict:
        """`max_accounts` caps the accounts Jupiter's route may use and `restrict_intermediate_tokens`
        keeps multi-hop routes on liquid intermediate tokens; both shrink the route so it fits in one
        transaction alongside a margin instruction's own accounts (Jupiter quote API options)."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps),
            "dexes": self._settings.jupiter_fork_dexes,
        }
        if max_accounts is not None:
            params["maxAccounts"] = str(max_accounts)
        if restrict_intermediate_tokens:
            params["restrictIntermediateTokens"] = "true"
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(f"{self._base_url}/swap/v1/quote", params=params, headers={**self._headers, "Accept": "application/json"})
        body = _safe_json(resp, "Jupiter quote failed")
        if resp.status_code != 200 or "error" in body:
            raise ContractCallError(f"Jupiter quote failed: {body.get('error', body)}")
        return body

    async def get_swap_instructions(
        self, quote: dict, user_pubkey: str, *, wrap_and_unwrap_sol: bool, use_shared_accounts: bool
    ) -> dict:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": wrap_and_unwrap_sol,
            "useSharedAccounts": use_shared_accounts,
        }
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                f"{self._base_url}/swap/v1/swap-instructions",
                json=payload,
                headers={**self._headers, "Content-Type": "application/json", "Accept": "application/json"},
            )
        body = _safe_json(resp, "Jupiter swap-instructions failed")
        if resp.status_code != 200 or "error" in body or "swapInstruction" not in body:
            raise ContractCallError(f"Jupiter swap-instructions failed: {body.get('error', body)}")
        return body

    async def get_swap_transaction(self, quote: dict, user_pubkey: str) -> dict:
        payload = {"quoteResponse": quote, "userPublicKey": user_pubkey}
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                f"{self._base_url}/swap/v1/swap",
                json=payload,
                headers={**self._headers, "Content-Type": "application/json", "Accept": "application/json"},
            )
        body = _safe_json(resp, "Jupiter swap failed")
        if resp.status_code != 200 or "error" in body or "swapTransaction" not in body:
            raise ContractCallError(f"Jupiter swap failed: {body.get('error', body)}")
        return body


def decode_jupiter_instruction(ix: dict) -> Instruction:
    """Jupiter's `{programId, accounts: [{pubkey, isSigner, isWritable}], data}`
    (base64 data) -> a solders Instruction. `isSigner` is always forced False
    here — for a margin swap the margin PDA can never actually sign at the
    outer transaction level (see `farm_kamino.py`'s module docstring on how
    `margin_execute`'s CPI signing actually works); for a wallet swap the
    real wallet signer is supplied separately by the caller, never through
    this account list."""
    accounts = [
        AccountMeta(Pubkey.from_string(a["pubkey"]), is_signer=False, is_writable=a["isWritable"])
        for a in ix["accounts"]
    ]
    return Instruction(
        program_id=Pubkey.from_string(ix["programId"]),
        data=base64.b64decode(ix["data"]),
        accounts=accounts,
    )


async def fetch_lookup_tables(client: SolanaClient, addresses: list[str]) -> list[AddressLookupTableAccount]:
    tables: list[AddressLookupTableAccount] = []
    for address in addresses:
        pubkey = Pubkey.from_string(address)
        data = await client.fetch_raw_account(pubkey)
        table = AddressLookupTable.deserialize(bytes(data))
        tables.append(AddressLookupTableAccount(key=pubkey, addresses=table.addresses))
    return tables
