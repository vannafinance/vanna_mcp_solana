"""Titan swaps: the second swap provider beside Jupiter.

A port of the app's `lib/solana/server/titan-api.ts` and `lib/solana/titan.ts` (vanna-credit-backend main `ea8a408`).
Titan's API returns ready-to-sign instructions with each quote, built for one address: the wallet for a wallet swap,
the margin PDA for a margin swap. A margin route must be Titan's own router (`swap_route_v3`, one instruction, config
3 = keep the input account open and keep wSOL output as wSOL, the margin as payer and user): that is all the Vanna
validator allows. The margin can't pay rent, so the wallet first creates any of Titan's "atlas" token accounts the route
needs. Titan's minimum out is its output less the slippage (the API returns no separate threshold).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal

import httpx
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import create_idempotent_associated_token_account

from vanna_core.client import SolanaClient
from vanna_core.pdas import find_associated_token_address
from vanna_core.phoenix import TITAN_ATLAS, TITAN_PROGRAM_ID
from vanna_core.types import ContractCallError

COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
SWAP_ROUTE_V3 = 42
MARGIN_CONFIG = 3  # KEEP_OUTPUT_WSOL | KEEP_INPUT_ACCOUNT: the only config the Vanna validator allows
FIXED_ACCOUNT_COUNT = 12


class TitanUnavailableError(ContractCallError):
    """No TITAN_API_KEY on this server: callers fall back to Jupiter quietly."""


@dataclass(frozen=True)
class TitanQuote:
    provider: str
    in_amount: int
    out_amount: int
    slippage_bps: int
    instructions: list[dict]  # {"p": program, "a": [{"p", "s", "w"}], "d": base64 data}
    address_lookup_tables: list[str]
    labels: list[str]

    @property
    def min_out(self) -> int:
        """Titan's own floor: the output less the slippage (the app's `titanAsQuote`)."""
        return self.out_amount * (10_000 - self.slippage_bps) // 10_000


class TitanClient:
    def __init__(self, settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(self._settings.titan_api_key or self._settings.titan_api_url)

    @property
    def base_url(self) -> str:
        base = (self._settings.titan_api_url or "https://portal.api.titan.exchange/api/v1").rstrip("/")
        return base if base.endswith("/api/v1") else f"{base}/api/v1"

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        user_public_key: str,
        slippage_bps: int,
        mode: Literal["wallet", "margin"],
    ) -> TitanQuote:
        if not self.configured:
            raise TitanUnavailableError("Titan is not configured on this server (no TITAN_API_KEY or TITAN_API_URL)")
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "userPublicKey": user_public_key,
            "slippageBps": str(slippage_bps),
            "titanSwapVersion": "3",
            "numQuotes": "1",
        }
        if mode == "margin":
            params.update(
                providers=self._settings.titan_providers,
                closeInputTokenAccount="false",
                createOutputTokenAccount="false",
                outputWsol="true",
            )
        else:
            params["createOutputTokenAccount"] = "true"
        params["dexes"] = self._settings.titan_fork_dexes
        key = self._settings.titan_api_key
        headers = {"Accept": "application/json", **({"x-api-key": key, "Authorization": f"Bearer {key}"} if key else {})}
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(f"{self.base_url}/quote/swap", params=params, headers=headers)
        if resp.status_code != 200:
            raise ContractCallError(f"Titan quote failed ({resp.status_code}): {resp.text[:200]}")
        return parse_titan_quote(resp.json(), amount_raw, slippage_bps)


def parse_titan_quote(body: dict, amount_raw: int, slippage_bps: int) -> TitanQuote:
    """The provider Titan expects to win (`metadata.expectedWinner`), else the first one it returned."""
    quotes = body.get("quotes") or {}
    metadata = body.get("metadata") or {}
    winner = metadata.get("expectedWinner") or metadata.get("ExpectedWinner")
    provider = winner if winner in quotes else next(iter(quotes), None)
    route = quotes.get(provider) if provider else None
    if not route:
        raise ContractCallError("Titan returned no route for this pair")
    steps = route.get("steps") or route.get("routePlan") or []
    return TitanQuote(
        provider=str(provider),
        in_amount=int(route.get("inAmount") or amount_raw),
        out_amount=int(route["outAmount"]),
        slippage_bps=int(route.get("slippageBps") or slippage_bps),
        instructions=list(route.get("instructions") or []),
        address_lookup_tables=[str(a) for a in route.get("addressLookupTables") or []],
        labels=[str(s.get("label") or s.get("dex") or s.get("amm") or "") for s in steps if isinstance(s, dict)],
    )


def _data(d: str) -> bytes:
    """Titan's base64 comes without padding (Node's `Buffer.from(d, "base64")` accepts that; Python needs it added)."""
    return base64.b64decode(d.replace("-", "+").replace("_", "/") + "=" * (-len(d) % 4))


def to_instruction(ix: dict) -> Instruction:
    return Instruction(
        program_id=Pubkey.from_string(ix["p"]),
        data=_data(ix["d"]),
        accounts=[AccountMeta(Pubkey.from_string(a["p"]), is_signer=bool(a["s"]), is_writable=bool(a["w"])) for a in ix["a"]],
    )


def margin_swap_call(quote: TitanQuote, margin: Pubkey) -> tuple[bytes, list[AccountMeta]]:
    """The route's one Titan swap, checked against what the Vanna validator allows; config forced to 3 and every
    account a non-signer (Vanna signs as the margin)."""
    swaps = [ix for ix in quote.instructions if ix.get("p") != COMPUTE_BUDGET_PROGRAM]
    if len(swaps) != 1 or swaps[0].get("p") != str(TITAN_PROGRAM_ID):
        raise ContractCallError(
            "Titan returned a route a margin account can't run (one Titan swap instruction only). Try Jupiter or another amount."
        )
    data = bytearray(_data(swaps[0]["d"]))
    if not data or data[0] != SWAP_ROUTE_V3:
        raise ContractCallError("Titan returned a route type the margin adapter doesn't allow")
    # The config byte is two flags (KEEP_OUTPUT_WSOL = 1, KEEP_INPUT_ACCOUNT = 2) that only decide whether wSOL accounts
    # get closed; Titan picks them per pair (1 for a SOL input, 2 for a SOL output). The margin's vaults must never be
    # closed, so any flag combination is forced to both: the one config the Vanna validator allows.
    if len(data) < 21 or data[1] & ~MARGIN_CONFIG:
        raise ContractCallError(f"Titan route config {data[1] if len(data) > 1 else '?'} isn't allowed for a margin account")
    data[1] = MARGIN_CONFIG
    accounts = [AccountMeta(Pubkey.from_string(a["p"]), is_signer=False, is_writable=bool(a["w"])) for a in swaps[0]["a"]]
    if len(accounts) < FIXED_ACCOUNT_COUNT or accounts[0].pubkey != margin or accounts[1].pubkey != margin:
        raise ContractCallError("Titan route is not built for the margin account as payer and user")
    return bytes(data), accounts


async def missing_atlas_accounts(client: SolanaClient, payer: Pubkey, accounts: list[AccountMeta], mints: int) -> list[Instruction]:
    """Create instructions (paid by the wallet) for Titan's atlas token accounts the route uses that don't exist yet.
    `mints` is the route's mint count (data byte 20): accounts [12, 12+n) are atlas accounts, [12+n, 12+2n) their mints."""
    atlas = [a.pubkey for a in accounts[FIXED_ACCOUNT_COUNT : FIXED_ACCOUNT_COUNT + mints]]
    mint_keys = [a.pubkey for a in accounts[FIXED_ACCOUNT_COUNT + mints : FIXED_ACCOUNT_COUNT + 2 * mints]]
    if not atlas:
        return []
    infos = await client.get_multiple_accounts_raw([*atlas, *mint_keys])
    created: list[Instruction] = []
    for account, mint in zip(atlas, mint_keys, strict=False):
        mint_info = infos.get(str(mint))
        if infos.get(str(account)) is not None or mint_info is None:
            continue
        token_program = Pubkey.from_string(mint_info[0])
        if find_associated_token_address(TITAN_ATLAS, mint, token_program) != account:
            continue
        created.append(create_idempotent_associated_token_account(payer, TITAN_ATLAS, mint, token_program))
    return created
