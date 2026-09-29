"""Wallet-level reads: balances, token metadata, connect status. Backs the
`vanna_wallet` dispatcher (TOOLS_CATALOGUE.md §3.13).
"""

from __future__ import annotations

import asyncio

from solders.pubkey import Pubkey

from vanna_core import math_utils
from vanna_core.assets import LEND_BORROW_SYMBOLS, AssetRegistry, DiscoveredAsset
from vanna_core.client import SolanaClient
from vanna_core.pdas import find_associated_token_address, margin_pda

TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class WalletContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client

    async def _token_balance(self, owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> int:
        ata = find_associated_token_address(owner, mint, token_program)
        try:
            data = await self._client.fetch_raw_account(ata)
        except Exception:  # noqa: BLE001 — no account yet == zero balance
            return 0
        return int.from_bytes(data[64:72], "little")

    async def _asset(self, symbol: str) -> DiscoveredAsset:
        registry = AssetRegistry(self._client)
        await registry.load()
        return registry.by_symbol(symbol)  # ValueError -> invalid_input for an unregistered symbol

    async def get_balance(self, owner: Pubkey, asset_symbol: str) -> dict:
        asset = await self._asset(asset_symbol)
        raw = await self._token_balance(owner, Pubkey.from_string(asset.mint), Pubkey.from_string(asset.token_program))
        return {"symbol": asset.symbol, "raw_balance": raw, "ui_balance": math_utils.ui_amount(raw, asset.decimals)}

    async def get_token_metadata(self, asset_symbol: str) -> dict:
        asset = await self._asset(asset_symbol)
        data = await self._client.fetch_raw_account(Pubkey.from_string(asset.mint))
        # SPL Mint layout: mint_authority_option(4) mint_authority(32) supply(8) decimals(1) ...
        supply = int.from_bytes(data[36:44], "little")
        decimals = data[44]
        return {
            "symbol": asset.symbol,
            "mint": asset.mint,
            "decimals": decimals,
            "supply_raw": supply,
            "supply_ui": math_utils.ui_amount(supply, decimals),
            "token_program": asset.token_program,
            "token_2022": asset.token_program == TOKEN_2022_PROGRAM_ID,
            "has_extensions": len(data) > 82,  # a classic Mint is exactly 82 bytes
            "collateral_enabled": asset.collateral_enabled,
            "lend_borrow_offered": asset.symbol.upper() in LEND_BORROW_SYMBOLS,
        }

    async def get_summary(self, owner: Pubkey) -> dict:
        from vanna_core.contracts.reserve import ReserveContract

        registry = AssetRegistry(self._client)
        sol_balance, margin_exists, _ = await asyncio.gather(
            self._get_native_sol(owner),
            self._client.account_exists(margin_pda(owner, self._client.program_id)[0]),
            registry.load(),
        )

        async def one_asset(asset: DiscoveredAsset) -> dict:
            mint = Pubkey.from_string(asset.mint)
            wallet_balance = await self._token_balance(owner, mint, Pubkey.from_string(asset.token_program))
            row = {
                "symbol": asset.symbol,
                "wallet_balance": math_utils.ui_amount(wallet_balance, asset.decimals),
                "lender_share_balance": None,
                "lender_redeemable_underlying": None,
            }
            # Earn (lend) balances exist only for the lend/borrow pools (product policy).
            if asset.symbol.upper() in LEND_BORROW_SYMBOLS and asset.has_reserve:
                reserve = ReserveContract(self._client, mint, asset.symbol, asset.decimals)
                deposited = await reserve.get_deposited_balance(owner)
                row["lender_share_balance"] = deposited["share_balance"]
                row["lender_redeemable_underlying"] = deposited["redeemable_underlying"]
            return row

        # Plain assets only: Kamino receipts live inside margin accounts, never in a wallet flow.
        plain = [a for a in registry.all() if not a.is_receipt]
        assets = await asyncio.gather(*(one_asset(a) for a in plain))

        return {
            "authority": str(owner),
            "sol_balance": sol_balance,
            "margin_account_exists": margin_exists,
            "margin_account": str(margin_pda(owner, self._client.program_id)[0]),
            "assets": list(assets),
        }

    async def _get_native_sol(self, owner: Pubkey) -> str:
        raw = await self._client.get_sol_balance(owner)
        return math_utils.ui_amount(raw, 9)
