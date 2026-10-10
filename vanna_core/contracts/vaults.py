"""Basis and Perena vaults through the margin account - a port of the app's vault code at vanna-credit-backend main
`2462473` (`lib/solana/vault-actions.ts`, `vault-state.ts`, `perena.ts`, `vault-oracles.ts`) and of the vault client it
vendors (`@vanna/phoenix-vault-client` 0.1.0: `math`, `state`, `jupiter`, `vault`).

What the app does, and so what this does:
- Every deposit is USDC; every withdrawal pays USDC. The vault share token is margin collateral (an allowed token).
- Perena USD* / USD* Junior: one `margin_execute` into Perena from the margin's USDC. No minimum-output parameter;
  Junior's instant withdrawal carries its 1% exit fee.
- Basis ETH/BTC/TSLA/META: one `margin_execute` into the Basis vault program; a deposit buys part of the USDC as the
  asset through a Jupiter route the vault runs itself (built for the vault authority), a withdrawal sells it back.
  The minimum output is the simulated output less 0.5%. The Phoenix book is refreshed (fork) before every Basis call.
- Basis SOL: the deployed vault takes the deposit from the WALLET (wallet USDC -> wallet shares); a second
  transaction (`move_shares`) deposits those shares into the margin account. Withdrawals are from margin, like the rest.
- APY is never invented: it comes from the app's hourly NAV history when the app is reachable, else it is empty.
"""

from __future__ import annotations

import asyncio

import base64
import logging
import struct
from dataclasses import dataclass

import borsh_construct as borsh
import httpx
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from spl.token.instructions import create_idempotent_associated_token_account

from vanna_core import constants, math_utils
from vanna_core.assets import AssetRegistry, DiscoveredAsset
from vanna_core.constants import EXEC_COMPUTE_UNITS
from vanna_core.client import SolanaClient, b58decode, compute_budget_prefix, request_heap_frame, set_compute_unit_limit
from vanna_core.contracts.jupiter import JupiterClient, fetch_lookup_tables
from vanna_core.envelope import WriteEnvelope
from vanna_core.margin_state import load_margin_state
from vanna_core.pdas import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID, find_associated_token_address, integration_pda, margin_pda
from vanna_core.phoenix import EMBER_PROGRAM_ID, PHOENIX_EXCHANGE, PHOENIX_LOG_AUTHORITY, PHOENIX_PROGRAM_ID, PhoenixExchange, _read_exchange, decode_perp_asset_map
from vanna_core.types import AccountNotFoundError, ContractCallError
from vanna_core.vault_pricing import VIEW_PAYER
from vanna_core.vaults import BASIS_PROGRAM, PERENA_PROGRAM, SHARE_DECIMALS, VAULTS, VaultInfo

logger = logging.getLogger(__name__)

PUBKEY = borsh.U8[32]
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
NATIVE_MINT = Pubkey.from_string(constants.WSOL_MINT)
USDC_MINT = Pubkey.from_string(constants.USDC_MINT)
JUPITER_PROGRAM_ID = Pubkey.from_string(constants.JUPITER_V6_PROGRAM)
HAWKEYE_PROGRAM_ID = Pubkey.from_string("RiSeVw3ZjNfsaXPRb4mgaqYaEEt41pNNJoDvVh7pgQj")
# The shared static Vanna/oracle accounts of the vault deployment (`lib/solana/vault-deployment.ts`).
VAULT_SHARED_LOOKUP_TABLE = "2NQE93vpXhM3VVLyEmYWQWHXWnXaxsfmab3VYym6dRqq"
BASIS = Pubkey.from_string(BASIS_PROGRAM)
PERENA = Pubkey.from_string(PERENA_PROGRAM)

BPS = 10_000
VIRTUAL_SHARES = 1_000_000
VIRTUAL_USD = 1_000_000
MULTIPLIER_SCALE = 10**12
MIN_OUT_BPS = 9_950  # the app's minimum output: the simulated output less 0.5%
VAULT_STALE_SECONDS = 3_600  # Perena NAV older than an hour: deposits closed (the app's `stale`)

# ── account layouts (Anchor; vanna_phoenix_vault IDL) ─────────────────────────────────────────────────

BASIS_VAULT_DISC = bytes([123, 245, 72, 41, 28, 175, 157, 209])
BASIS_CONFIG_DISC = bytes([155, 12, 170, 224, 30, 250, 204, 130])
DEPOSIT_DISC = bytes([242, 35, 198, 137, 82, 225, 242, 182])
WITHDRAW_DISC = bytes([183, 18, 70, 156, 148, 109, 161, 34])
SWAP_NOT_NEEDED = 6030  # the Basis program's SwapNotNeeded (a withdrawal with nothing to sell)

BasisVaultLayout = borsh.CStruct(
    "kind" / borsh.U8,  # 0 = NativeSol, 1 = Spot
    "asset_mint" / PUBKEY,
    "asset_decimals" / borsh.U8,
    "asset_token_program" / PUBKEY,
    "share_mint" / PUBKEY,
    "authority" / PUBKEY,
    "usdc_account" / PUBKEY,
    "phoenix_account" / PUBKEY,
    "asset_account" / PUBKEY,
    "trader_account" / PUBKEY,
    "asset_id" / borsh.U32,
    "orderbook" / PUBKEY,
    "spline_collection" / PUBKEY,
    "base_lot_decimals" / borsh.U8,
    "tick_size" / borsh.U64,
    "max_position_usd" / borsh.U64,
    "max_asset_units" / borsh.U64,
    "max_oi_share_bps" / borsh.U16,
    "max_deposit_usd" / borsh.U64,
    "slippage_bps" / borsh.U16,
    "keeper_band_bps" / borsh.U16,
    "max_rebalance_usd" / borsh.U64,
    "rebalance_window_start" / borsh.I64,
    "rebalance_window_usd" / borsh.U64,
    "margin_bps" / borsh.U16,
    "target_margin_bps" / borsh.U16,
    "deposits_open" / borsh.Bool,
    "after_hours_deposits" / borsh.Bool,
)
BasisConfigLayout = borsh.CStruct(
    "admin" / PUBKEY,
    "keeper" / PUBKEY,
    "usdc_mint" / PUBKEY,
    "phoenix_mint" / PUBKEY,
    "perp_asset_map" / PUBKEY,
    "withdraw_queue" / PUBKEY,
    "global_vault" / PUBKEY,
    "swap_program" / PUBKEY,
    "paused" / borsh.Bool,
)


def _pk(raw) -> Pubkey:
    return Pubkey(bytes(raw))


@dataclass(frozen=True)
class BasisVault:
    address: Pubkey
    spot: bool
    asset_mint: Pubkey
    asset_decimals: int
    asset_token_program: Pubkey
    share_mint: Pubkey
    authority: Pubkey
    usdc_account: Pubkey
    phoenix_account: Pubkey
    asset_account: Pubkey | None
    trader_account: Pubkey
    asset_id: int
    orderbook: Pubkey
    spline_collection: Pubkey
    base_lot_decimals: int
    tick_size: int
    max_position_usd: int
    max_deposit_usd: int
    target_margin_bps: int
    deposits_open: bool
    usdc_mint: Pubkey
    phoenix_mint: Pubkey
    paused: bool
    # False: the vault refuses deposits while its Phoenix market is after hours (stock markets outside their session).
    after_hours_deposits: bool = True


# The app's wording (backend lib/solana/vault-errors.ts MARKET_SESSION_MESSAGE).
MARKET_SESSION_MESSAGE = (
    "Deposits are closed while this market is outside its trading session or reopening. Try again when the session "
    "is open. Withdrawals remain subject to available liquidity."
)


async def market_after_hours(client: SolanaClient, ex: PhoenixExchange, asset_id: int) -> bool | None:
    """Whether the vault's Phoenix market is outside its session (the app's `vault-actions.ts` check before a Basis
    deposit). None when the market isn't in the perp asset map."""
    raw = await client.get_multiple_accounts_raw([ex.perp_asset_map])
    entry = raw.get(str(ex.perp_asset_map))
    market = next((m for m in decode_perp_asset_map(entry[1]) if m.asset_id == asset_id), None) if entry else None
    return None if market is None else market.after_hours


def basis_config_address() -> Pubkey:
    return Pubkey.find_program_address([b"config"], BASIS)[0]


async def load_basis_vault(client: SolanaClient, info: VaultInfo) -> BasisVault:
    address, config = Pubkey.from_string(info.account), basis_config_address()
    raw = await client.get_multiple_accounts_raw([address, config])
    v, c = raw.get(str(address)), raw.get(str(config))
    if v is None or v[0] != BASIS_PROGRAM or v[1][:8] != BASIS_VAULT_DISC:
        raise ContractCallError(f"{info.name} is not deployed on this network")
    if c is None or c[1][:8] != BASIS_CONFIG_DISC:
        raise ContractCallError("The Basis vault program's config was not found")
    d, cfg = BasisVaultLayout.parse(v[1][8:]), BasisConfigLayout.parse(c[1][8:])
    spot = int(d.kind) == 1
    vault = BasisVault(
        address=address, spot=spot, asset_mint=_pk(d.asset_mint), asset_decimals=int(d.asset_decimals),
        asset_token_program=_pk(d.asset_token_program), share_mint=_pk(d.share_mint), authority=_pk(d.authority),
        usdc_account=_pk(d.usdc_account), phoenix_account=_pk(d.phoenix_account),
        asset_account=_pk(d.asset_account) if spot else None, trader_account=_pk(d.trader_account),
        asset_id=int(d.asset_id), orderbook=_pk(d.orderbook), spline_collection=_pk(d.spline_collection),
        base_lot_decimals=int(d.base_lot_decimals), tick_size=int(d.tick_size), max_position_usd=int(d.max_position_usd),
        max_deposit_usd=int(d.max_deposit_usd), target_margin_bps=int(d.target_margin_bps),
        deposits_open=bool(d.deposits_open), usdc_mint=_pk(cfg.usdc_mint), phoenix_mint=_pk(cfg.phoenix_mint),
        paused=bool(cfg.paused), after_hours_deposits=bool(d.after_hours_deposits),
    )
    if str(vault.share_mint) != info.mint:
        raise ContractCallError(f"{info.name}: share mint mismatch")
    return vault


# ── the vault program's arithmetic (client `math.ts` = program `math.rs`, `nav.rs`, `swap.rs`) ──────────

def token_price(mark_ticks: int, tick_size: int, base_lot_decimals: int, multiplier: int = MULTIPLIER_SCALE) -> int:
    """USD (6 dp) per whole asset token at Phoenix mark `mark_ticks`, times a stock token's dividend multiplier."""
    return (mark_ticks * tick_size * 10**base_lot_decimals * multiplier) // MULTIPLIER_SCALE


def usd_value(amount: int, price: int, decimals: int) -> int:
    return (amount * price) // 10**decimals


def tokens_for_usd(usd: int, price: int, decimals: int) -> int:
    return (usd * 10**decimals) // price


def deposit_swap_amount(usdc_amount: int, target_margin_bps: int) -> int:
    """How much of a USDC deposit buys the asset: all but the margin the vault keeps (`swap::deposit_swap_amount`)."""
    return (usdc_amount * BPS) // (BPS + target_margin_bps)


def withdraw_swap_estimate(spot: bool, shares: int, supply: int, asset_units: int, nav: int, price: int, decimals: int) -> int:
    """About how much asset a withdrawal sells; the vault resizes the route to the exact amount."""
    if supply == 0:
        return 0
    if spot:
        return (asset_units * shares) // supply
    payout = ((nav * shares) // supply) * 999 // 1000
    return tokens_for_usd(payout, price, decimals)


def share_value(shares: int, nav: int, supply: int) -> int:
    return (shares * (nav + VIRTUAL_USD)) // (supply + VIRTUAL_SHARES)


def read_multiplier(mint_data: bytes, now: int) -> int:
    """A Token-2022 scaled-UI-amount multiplier (extension 25) over MULTIPLIER_SCALE; 1 for other tokens."""
    at = 166
    while len(mint_data) >= at + 4:
        kind, length = struct.unpack_from("<HH", mint_data, at)
        if kind == 25:
            v = mint_data[at + 4 : at + 4 + length]
            starts = struct.unpack_from("<q", v, 40)[0]
            multiplier = struct.unpack_from("<d", v, 48 if now >= starts else 32)[0]
            return int(multiplier * MULTIPLIER_SCALE)
        at += 4 + length
    return MULTIPLIER_SCALE


# ── reading a vault's value: Phoenix's Hawkeye views (simulated) ──────────────────────────────────────

VIEW_MARGIN = bytes.fromhex("b20a7cadecd27506")
VIEW_MARGIN_FOR_ASSET = bytes.fromhex("2012affea1ed4d9b")


def _hawkeye_accounts(trader: Pubkey, ex: PhoenixExchange) -> list[AccountMeta]:
    keys = [PHOENIX_PROGRAM_ID, PHOENIX_EXCHANGE, *ex.trader_index, *ex.active_buffer, ex.perp_asset_map, trader]
    return [AccountMeta(k, False, False) for k in keys]


async def _simulate_raw(client: SolanaClient, tx: VersionedTransaction, *, addresses: list[str] | None = None) -> dict:
    """simulateTransaction over JSON-RPC (inner instructions, post-balances of `addresses`) - options the Python RPC
    client does not expose. Returns the RPC `value`."""
    params: dict = {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "innerInstructions": True,
                    "commitment": "confirmed"}
    if addresses:
        params["accounts"] = {"encoding": "base64", "addresses": addresses}
    body = {"jsonrpc": "2.0", "id": 1, "method": "simulateTransaction",
            "params": [base64.b64encode(bytes(tx)).decode(), params]}
    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = (await http.post(client.settings.solana_rpc_url, json=body)).json()
    if "error" in resp:
        raise ContractCallError(f"Simulation request failed: {resp['error']}")
    return resp["result"]["value"]


async def _view(client: SolanaClient, ix: Instruction, size: int) -> bytes:
    blockhash = (await client._rpc.get_latest_blockhash()).value.blockhash
    tx = VersionedTransaction.populate(MessageV0.try_compile(VIEW_PAYER, [ix], [], blockhash), [Signature.default()])
    value = await _simulate_raw(client, tx)
    ret = (value.get("returnData") or {}).get("data")
    if value.get("err") is not None or not ret:
        raise ContractCallError(f"Phoenix view failed: {value.get('err')}")
    return (base64.b64decode(ret[0]) + bytes(size))[:size]  # Solana trims trailing zero bytes


@dataclass(frozen=True)
class VaultState:
    nav: int  # USD, 6 dp
    supply: int  # shares, 6 dp
    share_price: int  # USD per share, 6 dp
    asset_units: int
    asset_value: int
    token_price: int
    phoenix_equity: int
    short_notional: int


async def basis_vault_state(client: SolanaClient, vault: BasisVault, ex: PhoenixExchange) -> VaultState:
    """The vault's value the way the program reads it (client `getVaultState`)."""
    margin = await _view(client, Instruction(HAWKEYE_PROGRAM_ID, VIEW_MARGIN, _hawkeye_accounts(vault.trader_account, ex)), 112)
    asset = await _view(
        client,
        Instruction(HAWKEYE_PROGRAM_ID, VIEW_MARGIN_FOR_ASSET + struct.pack("<I", vault.asset_id) + bytes(4),
                    _hawkeye_accounts(vault.trader_account, ex)),
        128,
    )
    collateral, unrealized, funding = (struct.unpack_from("<q", margin, o)[0] for o in (16, 88, 104))
    base_lots = struct.unpack_from("<q", asset, 24)[0]
    mark_ticks = struct.unpack_from("<Q", asset, 40)[0]
    raw = await client.get_multiple_accounts_raw([vault.share_mint, vault.asset_mint])
    share_mint, asset_mint = raw.get(str(vault.share_mint)), raw.get(str(vault.asset_mint))
    if share_mint is None or asset_mint is None:
        raise ContractCallError("The vault's mints were not found")
    supply = struct.unpack_from("<Q", share_mint[1], 36)[0]
    multiplier = read_multiplier(asset_mint[1], await client.get_chain_clock_unix_timestamp())
    if vault.asset_account is not None:
        holding = await client.fetch_raw_account_or_none(vault.asset_account)
        asset_units = struct.unpack_from("<Q", holding, 64)[0] if holding and len(holding) >= 72 else 0
    else:
        # The SOL vault holds its SOL as the trader account's lamports above its rent floor.
        found = await client.fetch_account_with_lamports_or_none(vault.trader_account)
        if found is None:
            raise ContractCallError("The vault's trader account was not found")
        lamports, data = found
        floor = (await client._rpc.get_minimum_balance_for_rent_exemption(len(data))).value
        asset_units = max(lamports - floor, 0)
    price = token_price(mark_ticks, vault.tick_size, vault.base_lot_decimals, multiplier)
    asset_val = usd_value(asset_units, price, vault.asset_decimals)
    equity = asset_val + collateral + funding + unrealized
    nav = max(equity, 0)
    short_notional = abs(base_lots) * mark_ticks * vault.tick_size
    return VaultState(nav=nav, supply=supply, share_price=share_value(10**SHARE_DECIMALS, nav, supply),
                      asset_units=asset_units, asset_value=asset_val, token_price=price,
                      phoenix_equity=collateral + funding + unrealized, short_notional=short_notional)


# ── Perena (perena.ts) ─────────────────────────────────────────────────────────────────────────────────

PERENA_VAULT = Pubkey.from_string(constants.PERENA_USDSTAR_VAULT)
PERENA_VAULT_ORACLE = Pubkey.from_string("2YH9QB7qmEBgekeXH4C36L2dwMPfLdoPi5DDxeTi1kyP")
PERENA_TRANCHE_STATE = Pubkey.from_string(constants.PERENA_TRANCHE_STATE)
PERENA_FEE_VAULT = Pubkey.from_string("EAf7PeuCxhMN9gEhE1Gagmj28krmu3wEqCLMbqpSTVqg")
_PERENA_SELECTORS = {
    (False, "deposit"): bytes([247, 103, 46, 184, 88, 188, 56, 46]),
    (False, "withdraw"): bytes([255, 93, 15, 141, 187, 94, 246, 162]),
    (True, "deposit"): bytes([231, 224, 46, 207, 175, 45, 77, 148]),
    (True, "withdraw"): bytes([240, 82, 254, 246, 177, 29, 57, 126]),
}
_PERENA_JUNIOR = 0


def perena_call(info: VaultInfo, call: str, margin: Pubkey, amount: int) -> Instruction:
    """Perena's execute instruction for the margin (`perenaCallAccounts` / `perenaCallData`)."""
    tranche = info.id == "perena-junior"
    share_mint = Pubkey.from_string(info.mint)
    data = _PERENA_SELECTORS[(tranche, call)] + (bytes([_PERENA_JUNIOR]) if tranche else b"") + struct.pack("<Q", amount)

    def w(k: Pubkey) -> AccountMeta:
        return AccountMeta(k, False, True)

    def r(k: Pubkey) -> AccountMeta:
        return AccountMeta(k, False, False)

    margin_usdc = w(find_associated_token_address(margin, USDC_MINT, TOKEN_PROGRAM_ID))
    margin_shares = w(find_associated_token_address(margin, share_mint, TOKEN_PROGRAM_ID))
    vault_usdc = w(find_associated_token_address(PERENA_VAULT, USDC_MINT, TOKEN_PROGRAM_ID))
    fees = [w(PERENA_FEE_VAULT), w(find_associated_token_address(PERENA_FEE_VAULT, USDC_MINT, TOKEN_PROGRAM_ID))]
    programs = [r(TOKEN_PROGRAM_ID), r(TOKEN_PROGRAM_ID)]
    if not tranche:
        accounts = [r(margin), w(PERENA_VAULT), r(PERENA_VAULT_ORACLE), r(PERENA_TRANCHE_STATE), r(USDC_MINT),
                    w(share_mint), margin_usdc, vault_usdc, *fees, margin_shares, *programs]
    else:
        head = [w(margin), w(PERENA_VAULT), r(PERENA_VAULT_ORACLE), w(PERENA_TRANCHE_STATE), r(USDC_MINT), w(share_mint),
                margin_usdc, vault_usdc]
        tail = [*programs, r(ASSOCIATED_TOKEN_PROGRAM_ID), r(SYSTEM_PROGRAM_ID)]
        accounts = [*head, *fees, margin_shares, *tail] if call == "deposit" else [*head, margin_shares, *tail]
    return Instruction(PERENA, data, accounts)


# ── Basis instructions (client `vault.ts`) ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Route:
    """A Jupiter route the vault runs itself (client `jupiterRoute`)."""

    metas: list[AccountMeta]
    data: bytes
    lookup_tables: list[str]
    in_amount: int
    out_amount: int


# Jupiter's route account caps for a Basis vault swap, loosest first (the app's is 30). A larger swap can cross more of a
# pool's price ranges and need more accounts; when the transaction then passes 1232 bytes it is rebuilt with the next cap.
ROUTE_ACCOUNT_CAPS = (30, 22, 16)


async def jupiter_route(
    jupiter: JupiterClient, vault: BasisVault, sell: str, amount: int, dexes: str, max_accounts: int = ROUTE_ACCOUNT_CAPS[0],
) -> Route:
    """A route selling `amount` of USDC (`sell="usdc"`) or of the vault's asset for the other side, for the vault
    authority, paying into the vault's own token account. Only the swap instruction: the vault signs as its authority."""
    asset, usdc = (vault.asset_mint, vault.asset_token_program), (vault.usdc_mint, TOKEN_PROGRAM_ID)
    (in_mint, _), (out_mint, out_program) = (usdc, asset) if sell == "usdc" else (asset, usdc)
    quote = await jupiter.get_quote(str(in_mint), str(out_mint), amount, 100, max_accounts=max_accounts, dexes=dexes)
    destination = find_associated_token_address(vault.authority, out_mint, out_program)
    body = await jupiter.get_swap_instructions(
        quote, str(vault.authority), wrap_and_unwrap_sol=False, use_shared_accounts=True,
        destination_token_account=str(destination), skip_user_accounts_rpc_calls=True,
    )
    ix = body["swapInstruction"]
    if ix["programId"] != str(JUPITER_PROGRAM_ID):
        raise ContractCallError(f"Unexpected swap program {ix['programId']}")
    metas = [
        AccountMeta(Pubkey.from_string(a["pubkey"]), a["isSigner"] and a["pubkey"] != str(vault.authority), a["isWritable"])
        for a in ix["accounts"]
    ]
    return Route(metas=metas, data=base64.b64decode(ix["data"]), lookup_tables=list(body.get("addressLookupTableAddresses") or []),
                 in_amount=int(quote["inAmount"]), out_amount=int(quote["outAmount"]))


def _core_accounts(vault: BasisVault, ex: PhoenixExchange) -> list[AccountMeta]:
    ember_state = Pubkey.find_program_address([bytes(PHOENIX_PROGRAM_ID), b"state"], EMBER_PROGRAM_ID)[0]
    ember_vault = Pubkey.find_program_address([bytes(PHOENIX_PROGRAM_ID), b"vault"], EMBER_PROGRAM_ID)[0]

    def w(k):
        return AccountMeta(k, False, True)

    def r(k):
        return AccountMeta(k, False, False)

    return [
        r(basis_config_address()), w(vault.address), w(vault.authority), w(vault.share_mint), r(vault.usdc_mint),
        w(vault.phoenix_mint), w(vault.usdc_account), w(vault.phoenix_account),
        w(vault.asset_account) if vault.asset_account is not None else r(BASIS),  # optional: None = the program id
        r(vault.asset_mint), r(vault.asset_token_program), w(vault.trader_account),
        r(PHOENIX_PROGRAM_ID), r(PHOENIX_LOG_AUTHORITY), w(PHOENIX_EXCHANGE), r(HAWKEYE_PROGRAM_ID), r(EMBER_PROGRAM_ID),
        r(ember_state), w(ember_vault), w(ex.perp_asset_map), w(vault.orderbook), w(vault.spline_collection),
        w(ex.withdraw_queue), w(ex.global_vault),
    ]


def basis_call(vault: BasisVault, ex: PhoenixExchange, call: str, user: Pubkey, user_signs: bool, amount: int,
               minimum: int, route: Route | None) -> Instruction:
    """The vault's `deposit` (USDC -> shares; route required) or `withdraw` (shares -> USDC; route optional)."""
    def w(k):
        return AccountMeta(k, False, True)

    def r(k):
        return AccountMeta(k, False, False)

    wsol = find_associated_token_address(vault.authority, NATIVE_MINT, TOKEN_PROGRAM_ID)
    accounts = [
        AccountMeta(user, user_signs, True), *_core_accounts(vault, ex),
        w(find_associated_token_address(user, vault.share_mint, TOKEN_PROGRAM_ID)),
        w(find_associated_token_address(user, vault.usdc_mint, TOKEN_PROGRAM_ID)),
        r(JUPITER_PROGRAM_ID) if (call == "deposit" or route is not None) else r(BASIS),
        w(wsol) if (not vault.spot and (call == "deposit" or route is not None)) else r(BASIS),
        r(TOKEN_PROGRAM_ID), r(ASSOCIATED_TOKEN_PROGRAM_ID), r(SYSTEM_PROGRAM_ID),
        *(AccountMeta(k, False, True) for k in (*ex.trader_index, *ex.active_buffer)),
        *(route.metas if route else []),
    ]
    head = bytes([len(ex.trader_index), len(ex.active_buffer)]) + struct.pack("<QQ", amount, minimum)
    if call == "deposit":
        if route is None:
            raise ContractCallError("Deposit amount is too small for this vault's hedge swap — increase the amount")
        data = DEPOSIT_DISC + head + bytes([len(route.metas)]) + struct.pack("<I", len(route.data)) + route.data
    else:
        data = WITHDRAW_DISC + head + (
            bytes([1, len(route.metas)]) + struct.pack("<I", len(route.data)) + route.data if route else bytes([0])
        )
    return Instruction(BASIS, data, accounts)


def _token_amount(data: bytes | None) -> int:
    return struct.unpack_from("<Q", data, 64)[0] if data and len(data) >= 72 else 0


def _basis_failed_swap_not_needed(logs: list[str]) -> bool:
    return any(f"{BASIS_PROGRAM} failed" in line and (f"0x{SWAP_NOT_NEEDED:x}" in line or str(SWAP_NOT_NEEDED) in line) for line in logs)


# ── published yields the app's vault pages show (backend `dd46264`): public APIs, no key ──────────────

PERENA_API = "https://api.perena.org/api"
PHOENIX_API = "https://perp-api.phoenix.trade"
HOURS_PER_YEAR = 24 * 365
_DAY_MS = 86_400_000
_external: dict[str, tuple[float, object]] = {}


async def _cached(key: str, ttl: float, load):
    import time

    hit = _external.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        value = await load()
    except Exception:  # noqa: BLE001 - a published figure that can't be read is shown as unavailable, never guessed
        value = None
    if value is not None or hit is None:
        _external[key] = (time.monotonic(), value)
    return value if value is not None else (hit[1] if hit else None)


_VAULT_READ_CONCURRENCY = 4  # chain reads at once when listing vaults (the fork's RPC rate-limits bursts)
_APY_SOURCES = {"history": "history", "deposit-event": "deposit_event_estimate"}


def _iso(ms) -> str | None:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if isinstance(ms, (int, float)) and ms > 0 else None


def app_apy_fields(app: dict) -> dict:
    """The app's vault APY row (`/api/farm/vaults`, backend `fa6d2f6`) as this tool reports it: each figure with its
    source, so a deposit-event estimate is never presented as share-price history. A figure without a known source
    (an app that predates the field) is history, the app's only source then."""
    out: dict = {}
    for days in (7, 30):
        value = app.get(f"apy{days}")
        value = value if isinstance(value, (int, float)) else None
        source = app.get(f"apy{days}Source")
        out[f"apy_{days}d_pct"] = value
        out[f"apy_{days}d_source"] = None if value is None else _APY_SOURCES.get(source, "history" if source is None else str(source))
        out[f"apy_{days}d_baseline"] = _iso(app.get(f"apy{days}BaselineAt")) if out[f"apy_{days}d_source"] == "deposit_event_estimate" else None
    status = app.get("apyHistoryStatus")
    out["apy_history_status"] = status if status in ("collecting", "available", "unavailable") else None
    return out


async def perena_published_apy(vault_id: str) -> float | None:
    """Perena's own 7-day APY for USD* / USD* Junior (mainnet; the app's GET /api/farm/perena), percent."""
    key = "usdstarJunior" if vault_id == "perena-junior" else "usdstar"

    async def load():
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(f"{PERENA_API}/{key}/apy", headers={"Origin": "https://app.perena.org"})
        value = resp.json().get("apy") if resp.status_code == 200 else None
        return float(value) if isinstance(value, (int, float)) else None

    return await _cached(f"perena:{key}", 300.0, load)


def funding_stats(rates: list[tuple[int, float]]) -> dict:
    """Phoenix's hourly funding (percent) per UTC day, annualised, then the hours-weighted 7 / 30-day means and the
    negative days of the last 30 (the app's `fundingDays` / `fundingStats`)."""
    days: dict[int, list[float]] = {}
    for at_ms, pct in rates:
        days.setdefault(at_ms // _DAY_MS, []).append(pct)
    rows = [(sum(v) / len(v) * HOURS_PER_YEAR, len(v)) for _, v in sorted(days.items())]

    def avg(part):
        hours = sum(h for _, h in part)
        return sum(apr * h for apr, h in part) / hours if hours else None

    last30 = rows[-30:]
    return {"funding_apr_7d_pct": avg(rows[-7:]), "funding_apr_30d_pct": avg(last30),
            "negative_days_30": sum(1 for apr, _ in last30 if apr < 0), "days_30": len(last30)}


async def phoenix_funding(asset: str) -> dict | None:
    """Phoenix mainnet funding for a Basis vault's market over 90 days (the app's GET /api/farm/funding): what the
    vault's short has been paid, annualised. Positive = longs pay shorts."""
    import time

    async def load():
        start = int((time.time() - 90 * 86_400) * 1000)
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.get(f"{PHOENIX_API}/v1/funding/{asset}/rates", params={"startTime": start, "limit": 10000})
        if resp.status_code != 200:
            return None
        rates = []
        for p in resp.json().get("rates") or []:
            t = p.get("timestamp")
            t = float(t) if isinstance(t, (int, float)) else None
            pct = float(p.get("fundingRatePercentage")) if p.get("fundingRatePercentage") is not None else None
            if t is None or pct is None:
                continue
            rates.append((int(t * 1000 if t < 1e12 else t), pct))
        rates.sort()
        if not rates:
            return None
        return {**funding_stats(rates), "last_hourly_pct": rates[-1][1]}

    return await _cached(f"funding:{asset}", 600.0, load)


class VaultContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client
        self._jupiter = JupiterClient(client.settings)

    # -- reads ---------------------------------------------------------------------------------

    @staticmethod
    def find(vault_id: str) -> VaultInfo:
        """A vault by id ("basis-eth", "perena-junior"), share symbol ("vbETH", "USD*") or plain name ("eth basis",
        "junior")."""
        key = "".join(ch for ch in vault_id.lower() if ch.isalnum() or ch == "*")
        for v in VAULTS:
            names = {v.id, v.symbol, v.name, v.id.split("-", 1)[1]}
            if v.kind == "basis":
                names |= {f"{v.asset}basis", f"basis{v.asset}"}
            if v.id == "perena-usdstar":
                names |= {"usdstar", "usd*", "perena"}
            if v.id == "perena-junior":
                names |= {"junior", "usd*junior", "usdstarjunior", "perenajunior"}
            if key in {"".join(ch for ch in n.lower() if ch.isalnum() or ch == "*") for n in names}:
                return v
        raise ValueError(f"Unknown vault {vault_id!r}. Vaults: {', '.join(v.id for v in VAULTS)}")

    async def _app_apys(self) -> dict[str, dict]:
        """APY from the app's hourly NAV history (`/api/farm/vaults`); empty when the app isn't reachable."""
        base = (self._client.settings.perps_helper_url or self._client.settings.price_keeper_url or "").rstrip("/")
        if not base:
            return {}
        try:
            async with httpx.AsyncClient(timeout=20.0) as http:
                resp = await http.get(f"{base}/api/farm/vaults")
            rows = resp.json() if resp.status_code == 200 else []
            return {r["id"]: r for r in rows if isinstance(r, dict) and "id" in r}
        except Exception:  # noqa: BLE001 - APY is optional; it is never invented
            return {}

    async def _registered(self, info: VaultInfo, registry: AssetRegistry, allowed: list[int]) -> bool:
        from vanna_core.layouts import Integration

        asset = next((a for a in registry.all(include_unoffered=True) if a.mint == info.mint), None)
        if asset is None or not asset.collateral_enabled or asset.asset_index not in allowed:
            return False
        target = BASIS if info.kind == "basis" else PERENA
        integration = await self._client.fetch_raw_account_or_none(integration_pda(target, self._client.program_id)[0])
        return bool(integration) and bool(Integration.parse(integration[8:]).enabled)

    async def _live(self, info: VaultInfo, registry: AssetRegistry, allowed: list[int], ex: PhoenixExchange | None) -> dict:
        row: dict = {"id": info.id, "name": info.name, "symbol": info.symbol, "kind": info.kind, "asset": info.asset,
                     "deposit_token": "USDC", "registered": False, "available": False, "error": None,
                     "nav_usd": None, "share_price_usd": None, "deposits_open": False, "paused": False,
                     "max_deposit_usd": None, "cap_usd": None, "exit_fee_bps": 100 if info.id == "perena-junior" else 0,
                     "deposit_from": "wallet, then moved to margin" if info.id == "basis-sol" else "margin"}
        try:
            row["registered"] = await self._registered(info, registry, allowed)
            if info.kind == "perena":
                raw = await self._client.get_multiple_accounts_raw([Pubkey.from_string(info.account), Pubkey.from_string(info.mint)])
                source, mint = raw.get(info.account), raw.get(info.mint)
                if source is None or source[0] != PERENA_PROGRAM or mint is None or len(source[1]) < (info.price_at or 0) + 16:
                    raise ContractCallError("Perena vault state is unavailable")
                price = struct.unpack_from("<Q", source[1], info.price_at)[0]
                published = struct.unpack_from("<q", source[1], info.price_at + 8)[0]
                supply = struct.unpack_from("<Q", mint[1], 36)[0]
                now = await self._client.get_chain_clock_unix_timestamp()
                stale = abs(now - published) > VAULT_STALE_SECONDS
                row.update(share_price_usd=f"{price / 1e6:.6f}", nav_usd=f"{supply * price // 10**6 / 1e6:.2f}",
                           nav_updated_at=published, stale=stale, deposits_open=not stale, available=True)
            else:
                vault = await load_basis_vault(self._client, info)
                state = await basis_vault_state(self._client, vault, ex or await _read_exchange(self._client))
                row.update(
                    nav_usd=f"{state.nav / 1e6:.2f}", share_price_usd=f"{state.share_price / 1e6:.6f}",
                    hedge={"asset_units": state.asset_units / 10**vault.asset_decimals, "short_usd": f"{state.short_notional / 1e6:.2f}",
                           "phoenix_equity_usd": f"{state.phoenix_equity / 1e6:.2f}",
                           "net_delta_pct": (round((state.asset_value - state.short_notional) * 100 / state.short_notional, 4)
                                             if state.short_notional > 0 else None)},
                    cap_usd=f"{vault.max_position_usd / 1e6:.2f}" if vault.max_position_usd > 0 else None,
                    max_deposit_usd=f"{vault.max_deposit_usd / 1e6:.2f}" if vault.max_deposit_usd > 0 else None,
                    deposits_open=vault.deposits_open and not vault.paused, paused=vault.paused, available=True,
                )
                if not vault.after_hours_deposits and ex is not None:
                    after_hours = await market_after_hours(self._client, ex, vault.asset_id)
                    row["market_after_hours"] = after_hours
                    if after_hours:
                        row.update(deposits_open=False, deposit_blocked_reason=MARKET_SESSION_MESSAGE)
        except Exception as exc:  # noqa: BLE001 - one vault failing must not hide the others (the app's per-row error)
            row["error"] = str(exc)[:200]
        return row

    async def list_vaults(self) -> dict:
        from vanna_core.margin_state import allowed_tokens_of, load_protocol_config

        registry = AssetRegistry(self._client)
        await registry.load()
        allowed = allowed_tokens_of(await load_protocol_config(self._client))
        try:
            ex = await _read_exchange(self._client)
        except Exception:  # noqa: BLE001
            ex = None
        # Every vault's chain read and its published yield are independent: fetched concurrently (at most
        # _VAULT_READ_CONCURRENCY chain reads at a time), in catalogue order. Sequentially this took 25-30 s.
        apys_task = asyncio.create_task(self._app_apys())
        gate = asyncio.Semaphore(_VAULT_READ_CONCURRENCY)

        async def one(info: VaultInfo) -> dict:
            async def live() -> dict:
                async with gate:
                    return await self._live(info, registry, allowed, ex)

            published = perena_published_apy(info.id) if info.kind == "perena" else phoenix_funding(info.asset)
            row, extra = await asyncio.gather(live(), published)
            row["published_apy_7d_pct" if info.kind == "perena" else "funding"] = extra
            return row

        rows = list(await asyncio.gather(*(one(info) for info in VAULTS)))
        apys = await apys_task
        for info, row in zip(VAULTS, rows):
            row.update(app_apy_fields(apys.get(info.id) or {}))
        return {"vaults": rows, "apy_note": (
            "apy_7d_pct / apy_30d_pct are the app's vault APY. apy_*_source says what each is: 'history' = this vault's "
            "own share-price history on Vanna; 'deposit_event_estimate' = an approximate figure from the share price of "
            "past on-chain deposits since apy_*_baseline (integer share rounding makes it approximate - always call it "
            "an estimate); empty = not enough data yet (apy_history_status 'collecting', or 'unavailable' when the "
            "history couldn't be read). Perena vaults also carry Perena's own published 7-day APY (mainnet). Basis "
            "vaults carry Phoenix's funding for their market (annualised, mainnet): what the short is paid, not the "
            "vault's net yield. Nothing here is computed by the MCP.")}

    async def positions(self, authority: Pubkey) -> dict:
        """The wallet's vault shares in its margin account (collateral) and any left in the wallet (SOL Basis)."""
        from vanna_core.oracle_reader import OracleReader

        registry = AssetRegistry(self._client)
        await registry.load()
        margin = margin_pda(authority, self._client.program_id)[0]
        has_margin = await self._client.account_exists(margin)
        holders = [("margin", margin)] if has_margin else []
        holders.append(("wallet", authority))
        keys = [find_associated_token_address(h, Pubkey.from_string(v.mint), TOKEN_PROGRAM_ID) for _, h in holders for v in VAULTS]
        raw = await self._client.get_multiple_accounts_raw(keys)
        assets = {a.mint: a for a in registry.all(include_unoffered=True)}
        held: list[tuple[str, VaultInfo, int]] = []
        i = 0
        for where, _ in holders:
            for v in VAULTS:
                amount = _token_amount((raw.get(str(keys[i])) or (None, None))[1])
                i += 1
                if amount > 0:
                    held.append((where, v, amount))
        priced = [assets[v.mint] for _, v, _ in held if v.mint in assets]
        view = await OracleReader(self._client).view(priced) if priced else None
        rows = []
        for where, v, amount in held:
            row = {"vault": v.id, "name": v.name, "symbol": v.symbol, "held_in": where,
                   "shares": math_utils.ui_amount(amount, SHARE_DECIMALS)}
            asset = assets.get(v.mint)
            try:
                price = view.price(asset) if (view and asset) else None
                row["collateral_value_usd"] = math_utils.usd_value_ui(price.value_of(amount, SHARE_DECIMALS, False)) if price else None
            except Exception:  # noqa: BLE001
                row["collateral_value_usd"] = None
                row["valuation"] = "unavailable"
            if where == "wallet" and v.id == "basis-sol":
                row["next_step"] = "move_shares: deposit these shares into the margin account"
            rows.append(row)
        return {"has_margin_account": has_margin, "positions": rows}

    # -- writes --------------------------------------------------------------------------------

    async def deposit(self, authority: Pubkey, vault_id: str, amount_usdc) -> WriteEnvelope:
        return await self._prepare(authority, self.find(vault_id), "deposit", amount_usdc)

    async def withdraw(self, authority: Pubkey, vault_id: str, shares) -> WriteEnvelope:
        return await self._prepare(authority, self.find(vault_id), "withdraw", shares)

    async def preview(self, authority: Pubkey, vault_id: str, call: str, amount) -> dict:
        """What a deposit / withdrawal would do, from a simulation: shares or USDC out, the minimum enforced, the
        margin health factor after. Nothing is built for signing."""
        if call not in ("deposit", "withdraw"):
            raise ValueError("call must be deposit or withdraw")
        result = await self._prepare(authority, self.find(vault_id), call, amount, preview_only=True)
        return result  # type: ignore[return-value]

    async def move_shares(self, authority: Pubkey, vault_id: str, amount=None) -> WriteEnvelope:
        """Deposit vault shares held in the wallet into the margin account (the SOL Basis second step, or recovery)."""
        from vanna_core.contracts.margin_trade import MarginTradeContract

        info = self.find(vault_id)
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = next((a for a in registry.all(include_unoffered=True) if a.mint == info.mint), None)
        if asset is None:
            raise ContractCallError(f"{info.name} is not registered on Vanna")
        source = find_associated_token_address(authority, Pubkey.from_string(info.mint), TOKEN_PROGRAM_ID)
        held = _token_amount(await self._client.fetch_raw_account_or_none(source))
        units = held if amount in (None, "", "all") else math_utils.base_units(amount, SHARE_DECIMALS)
        if units <= 0 or units > held:
            raise ValueError(f"No {info.symbol} shares in the wallet to move" if held == 0 else
                             f"The wallet holds only {math_utils.ui_amount(held, SHARE_DECIMALS)} {info.symbol}")
        trade = MarginTradeContract(self._client)
        await trade._require_allowed(asset)
        margin = trade.margin_address(authority)
        instructions = trade._deposit_collateral_instructions(authority, margin, asset, math_utils.ui_amount(units, SHARE_DECIMALS))
        return await self._client.build_write_envelope(
            instructions, authority, purpose="user_deposit_collateral:vault_shares", compute_unit_limit=400_000,
            use_protocol_lookup_tables=True,
            summary={"action": "vault_move_shares", "vault": info.id, "symbol": info.symbol,
                     "shares": math_utils.ui_amount(units, SHARE_DECIMALS)},
        )

    async def _refresh(self, info: VaultInfo) -> None:
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices
        from vanna_core.contracts.perps import PerpsContract

        await maybe_refresh_prices(self._client, self._client.settings)
        if info.kind == "basis":
            # Basis enforces its price band against the fork's mark: the app refreshes the market data first.
            await PerpsContract(self._client).refresh_vault_book(info.asset)

    async def _prepare(self, authority: Pubkey, info: VaultInfo, call: str, amount, *, preview_only: bool = False,
                       attempt: int = 0, no_route: bool = False, route_accounts: int = ROUTE_ACCOUNT_CAPS[0]):
        from vanna_core import wallet_alt
        from vanna_core.margin_execute import build_margin_execute_instructions
        from vanna_core.protocol_alt import load_protocol_tables

        units = math_utils.base_units(amount, SHARE_DECIMALS)  # USDC and shares both have six decimals
        if units <= 0:
            raise ValueError("Enter an amount greater than zero")
        await self._refresh(info)
        registry = AssetRegistry(self._client)
        await registry.load()
        margin = margin_pda(authority, self._client.program_id)[0]
        try:
            state = await load_margin_state(self._client, margin, registry)
        except AccountNotFoundError as exc:
            raise AccountNotFoundError("Create a margin account first") from exc
        receipt = next((a for a in registry.all(include_unoffered=True) if a.mint == info.mint), None)
        usdc = registry.by_symbol("USDC")
        if receipt is None or not state.is_allowed(receipt.asset_index) or not state.is_allowed(usdc.asset_index):
            raise ContractCallError("This vault is not enabled on Vanna yet")
        wallet_deposit = info.id == "basis-sol" and call == "deposit"
        holder = authority if wallet_deposit else margin
        share_account = find_associated_token_address(holder, Pubkey.from_string(info.mint), TOKEN_PROGRAM_ID)
        cash_account = find_associated_token_address(holder, USDC_MINT, TOKEN_PROGRAM_ID)
        before = await self._client.get_multiple_accounts_raw([share_account, cash_account])
        share_before = _token_amount((before.get(str(share_account)) or (None, None))[1])
        cash_before = _token_amount((before.get(str(cash_account)) or (None, None))[1])
        if units > (cash_before if call == "deposit" else share_before):
            where = "wallet" if wallet_deposit else "margin account"
            have = math_utils.ui_amount(cash_before if call == "deposit" else share_before, SHARE_DECIMALS)
            raise ValueError(f"Insufficient {where} {'USDC' if call == 'deposit' else info.symbol}: {have} available")

        table_keys = [VAULT_SHARED_LOOKUP_TABLE, *([info.lookup_table] if info.lookup_table else [])]
        setup: list[Instruction] = []
        route: Route | None = None
        if info.kind == "perena":
            def outside(minimum: int) -> Instruction:
                return perena_call(info, call, margin, units)
        else:
            vault = await load_basis_vault(self._client, info)
            ex = await _read_exchange(self._client)
            if call == "deposit" and not vault.after_hours_deposits:
                # Checked before building, like the app: otherwise the program's MarketAfterHours surfaces as a bare
                # simulation failure.
                after_hours = await market_after_hours(self._client, ex, vault.asset_id)
                if after_hours is None:
                    raise ContractCallError("Market session status is unavailable. Please retry shortly.")
                if after_hours:
                    raise ContractCallError(MARKET_SESSION_MESSAGE)
            if call == "deposit" and (vault.paused or not vault.deposits_open):
                raise ContractCallError("Vault deposits are paused")
            if call == "deposit" and vault.max_deposit_usd > 0 and units > vault.max_deposit_usd:
                raise ValueError(f"Maximum deposit is {vault.max_deposit_usd / 1e6:.2f} USDC")
            if call == "deposit":
                sold = deposit_swap_amount(units, vault.target_margin_bps)
            else:
                vs = await basis_vault_state(self._client, vault, ex)
                sold = withdraw_swap_estimate(vault.spot, units, vs.supply, vs.asset_units, vs.nav, vs.token_price, vault.asset_decimals)
            dexes = "Raydium CLMM" if info.asset in ("TSLA", "META") else "Whirlpool"
            if sold > 0 and not no_route:
                route = await jupiter_route(self._jupiter, vault, "usdc" if call == "deposit" else "asset", sold, dexes, route_accounts)
                table_keys += route.lookup_tables
            if not vault.spot and call == "withdraw":
                setup.append(create_idempotent_associated_token_account(authority, vault.authority, NATIVE_MINT, TOKEN_PROGRAM_ID))
            if call == "deposit" and route is None:
                raise ContractCallError("Deposit amount is too small for this vault's hedge swap — increase the amount")

            def outside(minimum: int) -> Instruction:
                return basis_call(vault, ex, call, holder, wallet_deposit, units, minimum, route)

        async def build(minimum: int) -> list[Instruction]:
            ix = outside(minimum)
            if wallet_deposit:
                return [*setup, request_heap_frame(256 * 1024), ix]
            cpi = [AccountMeta(m.pubkey, False, m.is_writable) for m in ix.accounts]
            wrapped = await build_margin_execute_instructions(
                self._client, authority, margin, target_program=ix.program_id,
                spent=usdc if call == "deposit" else receipt, received=receipt if call == "deposit" else usdc,
                cpi_accounts=cpi, data=bytes(ix.data), registry=registry,
            )
            return [*setup, *wrapped]

        # Margin calls need the wallet's lookup table (it carries the margin's vaults) before they can fit.
        if not wallet_deposit and preview_only:
            if await wallet_alt.needs_setup(self._client, authority, registry):
                # A preview can't ask for a signature: say what comes first instead of failing on size.
                return {"action": f"vault_{call}", "vault": info.id, "name": info.name, "needs_setup": True,
                        "message": "This wallet's one-time lookup-table setup comes first: the deposit or withdraw "
                                   "action returns it to sign; preview again after it lands."}
        elif not wallet_deposit:
            try:
                prerequisite = await wallet_alt.setup_envelope_if_needed(self._client, authority, registry)
            except wallet_alt.WalletTableNotReady as exc:
                raise ContractCallError(str(exc)) from exc
            if prerequisite is not None:
                prerequisite.summary["then"] = f"vault_{call}"
                return prerequisite
        tables = await fetch_lookup_tables(self._client, list(dict.fromkeys(table_keys)))
        given = {t.key for t in tables}
        tables += [t for t in await load_protocol_tables(self._client, self._client.settings.protocol_lookup_tables) if t.key not in given]
        wallet_table = None if wallet_deposit else await wallet_alt.load_wallet_table(self._client, authority)
        if wallet_table is not None and wallet_table.key not in {t.key for t in tables}:
            tables.append(wallet_table)

        trial = await build(0)
        full = [*compute_budget_prefix(trial, self._client.program_id, EXEC_COMPUTE_UNITS), *trial]
        blockhash = (await self._client._rpc.get_latest_blockhash()).value.blockhash
        message = MessageV0.try_compile(authority, full, tables, blockhash)
        tx = VersionedTransaction.populate(message, [Signature.default()] * message.header.num_required_signatures)
        if len(bytes(tx)) > 1232:
            tighter = [c for c in ROUTE_ACCOUNT_CAPS if c < route_accounts]
            if route is not None and tighter:
                return await self._prepare(authority, info, call, amount, preview_only=preview_only, attempt=attempt,
                                           no_route=no_route, route_accounts=tighter[0])
            raise ContractCallError(
                f"This {call} needs a larger swap route than fits in one Solana transaction ({len(bytes(tx))} bytes; the "
                f"limit is 1232). A smaller amount needs a smaller route: try a smaller {call}, or split it into two."
            )
        sim = await _simulate_raw(self._client, tx, addresses=[str(share_account), str(cash_account)])
        logs = sim.get("logs") or []
        if sim.get("err") is not None and attempt < 2:
            if call == "withdraw" and info.kind == "basis" and _basis_failed_swap_not_needed(logs):
                return await self._prepare(authority, info, call, amount, preview_only=preview_only, attempt=attempt + 1, no_route=True)
            from vanna_core.fork_heal import heal_stale_dex_pools

            if route is not None and await heal_stale_dex_pools(self._client, tx) > 0:
                return await self._prepare(authority, info, call, amount, preview_only=preview_only, attempt=attempt + 1, no_route=no_route,
                                           route_accounts=route_accounts)
        if sim.get("err") is not None:
            detail = " ".join(line for line in logs if any(k in line for k in ("Error Message:", "failed:", "Error:")))[-600:]
            raise ContractCallError(f"Vault preview failed: {sim.get('err')}. {detail}")
        after = [_token_amount(base64.b64decode(a["data"][0])) if a else 0 for a in (sim.get("accounts") or [])]
        if len(after) != 2:
            raise ContractCallError("The RPC did not return the simulated balances")
        share_change, cash_change = after[0] - share_before, after[1] - cash_before
        output = share_change if call == "deposit" else cash_change
        if output <= 0:
            raise ContractCallError("The vault cannot fill this amount now")
        health_factor = _health_after(sim, message, tables, self._client.program_id)
        minimum = output * MIN_OUT_BPS // BPS
        out_symbol = info.symbol if call == "deposit" else "USDC"
        summary = {
            "action": f"vault_{call}", "vault": info.id, "name": info.name,
            "amount": str(amount), "amount_token": "USDC" if call == "deposit" else info.symbol,
            "expected_out": math_utils.ui_amount(output, SHARE_DECIMALS), "out_token": out_symbol,
            "minimum_out": math_utils.ui_amount(minimum if info.kind == "basis" else output, SHARE_DECIMALS),
            "health_factor_after": health_factor,
            "from": "wallet" if wallet_deposit else "margin",
        }
        if info.id == "perena-junior" and call == "withdraw":
            summary["note"] = "USD* Junior's instant withdrawal includes its 1% exit fee (already in the expected USDC)."
        if info.kind == "perena":
            summary["note"] = (summary.get("note", "") + " Perena takes no minimum-output parameter.").strip()
        if wallet_deposit:
            summary["next_step"] = "move_shares: after this lands, deposit the new shares into the margin account"
        if preview_only:
            return summary
        instructions = await build(minimum if info.kind == "basis" else 0)
        return await self._client.build_write_envelope(
            instructions, authority, purpose=f"vault_{call}", compute_unit_limit=EXEC_COMPUTE_UNITS,
            address_lookup_tables=tables, use_protocol_lookup_tables=not wallet_deposit, summary=summary,
        )


def _health_after(sim: dict, message: MessageV0, tables, program_id: Pubkey) -> str | None:
    """The margin's health factor after the call, from the simulated MarginExecuted CPI event (None = no debt)."""
    from vanna_core.client import EVENT_IX_TAG
    from vanna_core.layouts import EVENT_LAYOUTS, event_discriminator

    keys = [str(k) for k in message.account_keys]
    for lookup in message.address_table_lookups:
        table = next((t for t in tables if t.key == lookup.account_key), None)
        if table is not None:
            keys += [str(table.addresses[i]) for i in lookup.writable_indexes]
    for lookup in message.address_table_lookups:
        table = next((t for t in tables if t.key == lookup.account_key), None)
        if table is not None:
            keys += [str(table.addresses[i]) for i in lookup.readonly_indexes]
    disc = event_discriminator("MarginExecuted")
    for group in sim.get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            # simulateTransaction reports inner instructions either parsed ({"programId": ...}) or compiled
            # ({"programIdIndex": n} into the static + loaded keys).
            index = ix.get("programIdIndex")
            program = ix.get("programId") or (keys[index] if index is not None and index < len(keys) else None)
            if program != str(program_id):
                continue
            try:
                data = b58decode(ix.get("data") or "")
            except Exception:  # noqa: BLE001
                continue
            if data[:8] == EVENT_IX_TAG and data[8:16] == disc:
                wad = EVENT_LAYOUTS["MarginExecuted"].parse(data[16:]).borrow_health_factor_wad
                return None if wad > 10**30 else f"{wad / 10**18:.4f}"
    return None


__all__ = ["VaultContract", "VAULTS", "basis_vault_state", "load_basis_vault", "set_compute_unit_limit"]
