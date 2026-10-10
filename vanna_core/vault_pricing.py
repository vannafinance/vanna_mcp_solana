"""Prices of the Basis and Perena vault share tokens, held as margin collateral since the 2026-10-09 upgrade - the app's
`lib/solana/vault-oracles.ts` and the receipt branch of `lib/solana/oracle.ts` (vanna-credit-backend main `2462473`).

- Perena (USD*, USD* Junior): the pool account's NAV, a u64 with 6 decimals and its i64 publish time at `price_at`.
  The price book names that account (its `klend_reserve` field, under the Perena program).
- Basis (vbSOL, vbETH, vbBTC, vbTSLA, vbMETA): the oracle agent's own conservative collateral price, read by
  simulating its `GetPrice` for one share with the vault's accounts (vault, share mint, the vault's Phoenix trader, its
  asset account, the Phoenix market book / exchange / asset map, the collateral and market index price accounts). The
  same accounts go in the oracle segment of any health-checked write while a Basis share is held.

Account membership is cached five minutes like the app; prices are always read fresh.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import borsh_construct as borsh
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from vanna_core.pricing import CONFIDENCE_OK, FRESH, TWAP_OK, OraclePrice, Price, PriceUnavailableError
from vanna_core.vaults import BASIS_PROGRAM, PERENA_PROGRAM, VaultInfo, vault_by_mint

PUBKEY = borsh.U8[32]
# The leading fields of `vanna_phoenix_vault::BasisVault` (Anchor; parsing stops after `asset_id`).
BasisVaultHead = borsh.CStruct(
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
)
BASIS_VAULT_DISC = bytes([123, 245, 72, 41, 28, 175, 157, 209])
VAULT_KIND_SPOT = 1
GET_PRICE_DISC = bytes([238, 38, 193, 106, 228, 32, 210, 33])
# Any funded-or-not key works as the payer of a simulation-only read (the app's VIEW_PAYER).
VIEW_PAYER = Pubkey.from_string("B7rDiCsL9DAWcd144RxPAiryc6AfVMHmq5n6KtiRmU8R")
_ACCOUNTS_TTL_SECS = 300.0
_accounts_cache: dict[str, tuple[float, list[str]]] = {}


@dataclass(frozen=True)
class BasisVaultState:
    address: Pubkey
    kind: int
    share_mint: Pubkey
    asset_account: Pubkey
    trader_account: Pubkey
    asset_id: int


def decode_basis_vault(address: Pubkey, data: bytes) -> BasisVaultState:
    if data[:8] != BASIS_VAULT_DISC:
        raise PriceUnavailableError("The Basis vault account has an unexpected layout")
    v = BasisVaultHead.parse(data[8:])
    return BasisVaultState(
        address=address, kind=int(v.kind), share_mint=Pubkey(bytes(v.share_mint)),
        asset_account=Pubkey(bytes(v.asset_account)), trader_account=Pubkey(bytes(v.trader_account)),
        asset_id=int(v.asset_id),
    )


def is_vault_receipt(asset) -> bool:
    return vault_by_mint(asset.mint) is not None and asset.oracle.uses_vault_receipt


def is_basis(asset) -> bool:
    vault = vault_by_mint(asset.mint)
    return vault is not None and vault.kind == "basis"


async def basis_oracle_accounts(client, vault: VaultInfo) -> list[str]:
    """Every account the oracle agent reads to value this vault's share (`basisOracleAccounts`)."""
    from vanna_core.phoenix import PHOENIX_EXCHANGE, _phoenix_statics

    key = f"{client.settings.solana_rpc_url}|{vault.id}"
    hit = _accounts_cache.get(key)
    if hit and time.monotonic() - hit[0] < _ACCOUNTS_TTL_SECS:
        return hit[1]
    raw = await client.get_multiple_accounts_raw([Pubkey.from_string(vault.account)])
    entry = raw.get(vault.account)
    if entry is None or entry[0] != BASIS_PROGRAM:
        raise PriceUnavailableError(f"{vault.name} is not deployed on this network")
    state = decode_basis_vault(Pubkey.from_string(vault.account), entry[1])
    if str(state.share_mint) != vault.mint:
        raise PriceUnavailableError(f"{vault.name}: share mint mismatch")
    statics = await _phoenix_statics(client)
    if statics is None:
        raise PriceUnavailableError("The Phoenix oracle market book is missing")
    book, exchange, _ = statics
    market = next((m for m in book.markets if m.asset_id == state.asset_id), None)
    if market is None:
        raise PriceUnavailableError(f"{vault.asset} is not registered in the Phoenix oracle market book")
    keys = [
        str(state.address), str(state.share_mint), str(state.trader_account),
        *([str(state.asset_account)] if state.kind == VAULT_KIND_SPOT else []),
        str(book.address), str(PHOENIX_EXCHANGE), str(exchange.perp_asset_map),
        *book.collateral_price.accounts(), str(market.market), *market.index_price.accounts(),
    ]
    keys = list(dict.fromkeys(keys))
    _accounts_cache[key] = (time.monotonic(), keys)
    return keys


async def basis_price(client, vault: VaultInfo) -> OraclePrice:
    """The oracle agent's price for one share (exponent -9), with the checks it reported (`basisOraclePrice`)."""
    from vanna_core.pdas import price_book_pda

    oracle = Pubkey.from_string(client.settings.vanna_oracle_program_id)
    accounts = await basis_oracle_accounts(client, vault)
    keys = list(dict.fromkeys([str(price_book_pda(oracle)[0]), *accounts]))
    query = bytearray(4 + 32 + 8 + 1 + 32)
    query[0:4] = (1).to_bytes(4, "little")
    query[4:36] = bytes(Pubkey.from_string(vault.mint))
    query[36:44] = (10**6).to_bytes(8, "little")  # one share (six decimals)
    ix = Instruction(oracle, GET_PRICE_DISC + bytes(query), [AccountMeta(Pubkey.from_string(k), False, False) for k in keys])
    blockhash = (await client._rpc.get_latest_blockhash()).value.blockhash
    message = MessageV0.try_compile(VIEW_PAYER, [ix], [], blockhash)
    tx = VersionedTransaction.populate(message, [Signature.default()])
    result = await client.simulate_versioned_transaction(tx)
    ret = result.value.return_data
    if result.value.err is not None or ret is None or str(ret.program_id) != str(oracle):
        raise PriceUnavailableError(f"{vault.name} collateral valuation is unavailable")
    data = bytes(ret.data) + bytes(38)
    if int.from_bytes(data[0:4], "little") != 1:
        raise PriceUnavailableError("Invalid oracle result count")
    value = int.from_bytes(data[20:36], "little")
    if value <= 0:
        raise PriceUnavailableError(f"{vault.name} has no value right now")
    status = data[36] & (FRESH | TWAP_OK | CONFIDENCE_OK)
    now = await client.get_chain_clock_unix_timestamp()
    return OraclePrice(price=Price.new(value, -9), receipt_rate=None, timestamp=now, status=status, source="receipt")


def perena_price(oracle, mint: str, accounts: dict, now: int) -> OraclePrice:
    """USD* / Junior: the pool's NAV (6 decimals) and publish time at `price_at` of the account the price book names."""
    vault = vault_by_mint(mint)
    if vault is None or vault.price_at is None:
        raise PriceUnavailableError("price_unavailable: unknown Perena receipt")
    account = accounts.get(oracle.klend_reserve)
    if account is None or account.owner != PERENA_PROGRAM or len(account.data) < vault.price_at + 16:
        raise PriceUnavailableError(f"price_unavailable: {vault.name} NAV account is missing")
    nav = int.from_bytes(account.data[vault.price_at : vault.price_at + 8], "little")
    published = int.from_bytes(account.data[vault.price_at + 8 : vault.price_at + 16], "little", signed=True)
    fresh = abs(now - published) <= oracle.max_age_secs
    return OraclePrice(
        price=Price.new(nav, -6), receipt_rate=None, timestamp=published,
        status=(FRESH if fresh else 0) | TWAP_OK | CONFIDENCE_OK, source="receipt",
    )


async def vault_oracle_keys(client, assets) -> list[str]:
    """The extra oracle-segment accounts for held Basis shares (Perena's NAV account is already its price account)."""
    keys: list[str] = []
    for asset in assets:
        vault = vault_by_mint(asset.mint)
        if vault is not None and vault.kind == "basis":
            keys += await basis_oracle_accounts(client, vault)
    return list(dict.fromkeys(keys))


def clear_vault_pricing_cache() -> None:
    _accounts_cache.clear()
