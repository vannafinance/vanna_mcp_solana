"""Kamino Lend farm writes — `deposit_reserve_liquidity` / `redeem_reserve_collateral`
via `margin_execute`. Backs `vanna_farm_kamino` (TOOLS_CATALOGUE.md §3.10).

Every account — Vanna's named `margin_execute` accounts, the 12 Kamino CPI
accounts (order, writable flags), and both instruction discriminators — was
verified against Kamino's own on-chain Anchor IDL (fetched live from its
IDL PDA, `kamino_lending` v1.25.0) and cross-checked against
`adapters/kamino.rs`'s account-order documentation and discriminator
constants; not assumed from either source alone.

A deposit's `spent` asset (the underlying) must already be an *active
collateral* position (the on-chain instruction requires this) — farm this
from an amount already deposited via `vanna_margin_trade.deposit`, not a
fresh wallet balance.
"""

from __future__ import annotations

from solders.instruction import AccountMeta
from solders.pubkey import Pubkey

from vanna_core import math_utils
from vanna_core.assets import (
    KAMINO_INSTRUCTIONS_SYSVAR,
    KAMINO_MAIN_MARKET,
    KAMINO_MAIN_MARKET_AUTHORITY,
    KLEND_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    AssetRegistry,
    DiscoveredAsset,
    receipt_for_underlying,
)
from vanna_core.client import SolanaClient
from vanna_core.contracts.kamino import KaminoContract
from vanna_core.envelope import WriteEnvelope
from vanna_core.pdas import (
    find_associated_token_address,
    margin_pda,
)
from vanna_core.margin_execute import build_margin_execute_instruction

# Verified against Kamino's own on-chain IDL (kamino_lending v1.25.0).
KAMINO_DEPOSIT_RESERVE_LIQUIDITY_DISC = bytes([169, 201, 30, 126, 6, 205, 102, 68])
KAMINO_REDEEM_RESERVE_COLLATERAL_DISC = bytes([234, 117, 181, 125, 185, 142, 220, 29])

DEFAULT_SLIPPAGE_BPS = 50  # 0.5%, TOOLS_CATALOGUE.md §2.6


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


class FarmKaminoContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client
        self._kamino = KaminoContract(client)

    async def _receipt_pair(self, underlying_symbol: str, registry: AssetRegistry) -> tuple[DiscoveredAsset, DiscoveredAsset]:
        receipt_info = receipt_for_underlying(underlying_symbol)
        if receipt_info is None:
            raise ValueError(f"{underlying_symbol!r} has no Kamino receipt registered")
        underlying = registry.by_symbol(underlying_symbol)
        receipt = registry.by_symbol(receipt_info.symbol)
        return underlying, receipt

    async def deposit(self, authority: Pubkey, underlying_symbol: str, amount_ui) -> WriteEnvelope:
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices

        await maybe_refresh_prices(self._client, self._client.settings)
        registry = AssetRegistry(self._client)
        await registry.load()
        underlying, receipt = await self._receipt_pair(underlying_symbol, registry)
        raw_amount = math_utils.base_units(amount_ui, underlying.decimals)
        reserve = Pubkey.from_string(receipt.oracle.klend_reserve)

        rate = await self._kamino.read_reserve_rate(reserve, KLEND_PROGRAM_ID, Pubkey.from_string(receipt.mint))
        expected_received = rate.receipts_for_underlying(raw_amount)
        min_received = math_utils.mul_div_floor(expected_received, 10_000 - DEFAULT_SLIPPAGE_BPS, 10_000)

        margin = margin_pda(authority, self._client.program_id)[0]
        cpi_accounts = self._deposit_cpi_accounts(margin, underlying, receipt, reserve)
        data = KAMINO_DEPOSIT_RESERVE_LIQUIDITY_DISC + _u64(raw_amount)

        return await self._build_and_submit(
            authority, margin, spent=underlying, received=receipt, registry=registry,
            cpi_accounts=cpi_accounts, data=data, min_received=min_received,
            purpose="margin_execute:kamino_deposit",
            summary={"action": "farm_deposit", "underlying": underlying.symbol, "amount": str(amount_ui)},
        )

    async def redeem(self, authority: Pubkey, underlying_symbol: str, receipt_amount_ui) -> WriteEnvelope:
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices

        await maybe_refresh_prices(self._client, self._client.settings)
        registry = AssetRegistry(self._client)
        await registry.load()
        underlying, receipt = await self._receipt_pair(underlying_symbol, registry)
        raw_amount = math_utils.base_units(receipt_amount_ui, receipt.decimals)
        reserve = Pubkey.from_string(receipt.oracle.klend_reserve)

        rate = await self._kamino.read_reserve_rate(reserve, KLEND_PROGRAM_ID, Pubkey.from_string(receipt.mint))
        expected_received = rate.underlying_for_receipts(raw_amount)
        min_received = math_utils.mul_div_floor(expected_received, 10_000 - DEFAULT_SLIPPAGE_BPS, 10_000)

        margin = margin_pda(authority, self._client.program_id)[0]
        cpi_accounts = self._redeem_cpi_accounts(margin, underlying, receipt, reserve)
        data = KAMINO_REDEEM_RESERVE_COLLATERAL_DISC + _u64(raw_amount)

        return await self._build_and_submit(
            authority, margin, spent=receipt, received=underlying, registry=registry,
            cpi_accounts=cpi_accounts, data=data, min_received=min_received,
            purpose="margin_execute:kamino_redeem",
            summary={"action": "farm_redeem", "underlying": underlying.symbol, "receipt_amount": str(receipt_amount_ui)},
        )

    def _deposit_cpi_accounts(
        self, margin: Pubkey, underlying: DiscoveredAsset, receipt: DiscoveredAsset, reserve: Pubkey
    ) -> list[AccountMeta]:
        spent_vault = find_associated_token_address(margin, Pubkey.from_string(underlying.mint), TOKEN_PROGRAM_ID)
        received_vault = find_associated_token_address(margin, Pubkey.from_string(receipt.mint), TOKEN_PROGRAM_ID)
        liquidity_vault = self._kamino_liquidity_vault(underlying.symbol)
        return [
            AccountMeta(margin, is_signer=False, is_writable=False),  # 0 owner
            AccountMeta(reserve, is_signer=False, is_writable=True),  # 1 reserve
            AccountMeta(KAMINO_MAIN_MARKET, is_signer=False, is_writable=False),  # 2 lending_market
            AccountMeta(KAMINO_MAIN_MARKET_AUTHORITY, is_signer=False, is_writable=False),  # 3 authority PDA
            AccountMeta(Pubkey.from_string(underlying.mint), is_signer=False, is_writable=False),  # 4 liquidity mint
            AccountMeta(liquidity_vault, is_signer=False, is_writable=True),  # 5 reserve_liquidity_supply
            AccountMeta(Pubkey.from_string(receipt.mint), is_signer=False, is_writable=True),  # 6 collateral mint
            AccountMeta(spent_vault, is_signer=False, is_writable=True),  # 7 user_source_liquidity
            AccountMeta(received_vault, is_signer=False, is_writable=True),  # 8 user_destination_collateral
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),  # 9 collateral_token_program
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),  # 10 liquidity_token_program
            AccountMeta(KAMINO_INSTRUCTIONS_SYSVAR, is_signer=False, is_writable=False),  # 11 instructions sysvar
        ]

    def _redeem_cpi_accounts(
        self, margin: Pubkey, underlying: DiscoveredAsset, receipt: DiscoveredAsset, reserve: Pubkey
    ) -> list[AccountMeta]:
        spent_vault = find_associated_token_address(margin, Pubkey.from_string(receipt.mint), TOKEN_PROGRAM_ID)
        received_vault = find_associated_token_address(margin, Pubkey.from_string(underlying.mint), TOKEN_PROGRAM_ID)
        liquidity_vault = self._kamino_liquidity_vault(underlying.symbol)
        return [
            AccountMeta(margin, is_signer=False, is_writable=False),  # 0 owner
            AccountMeta(KAMINO_MAIN_MARKET, is_signer=False, is_writable=False),  # 1 lending_market
            AccountMeta(reserve, is_signer=False, is_writable=True),  # 2 reserve
            AccountMeta(KAMINO_MAIN_MARKET_AUTHORITY, is_signer=False, is_writable=False),  # 3 authority PDA
            AccountMeta(Pubkey.from_string(underlying.mint), is_signer=False, is_writable=False),  # 4 liquidity mint
            AccountMeta(Pubkey.from_string(receipt.mint), is_signer=False, is_writable=True),  # 5 collateral mint
            AccountMeta(liquidity_vault, is_signer=False, is_writable=True),  # 6 reserve_liquidity_supply
            AccountMeta(spent_vault, is_signer=False, is_writable=True),  # 7 user_source_collateral
            AccountMeta(received_vault, is_signer=False, is_writable=True),  # 8 user_destination_liquidity
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),  # 9 collateral_token_program
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),  # 10 liquidity_token_program
            AccountMeta(KAMINO_INSTRUCTIONS_SYSVAR, is_signer=False, is_writable=False),  # 11 instructions sysvar
        ]

    @staticmethod
    def _kamino_liquidity_vault(underlying_symbol: str) -> Pubkey:
        from vanna_core.assets import KAMINO_RECEIPTS

        for r in KAMINO_RECEIPTS:
            if r.underlying_symbol.upper() == underlying_symbol.upper():
                return Pubkey.from_string(r.liquidity_vault)
        raise ValueError(f"No Kamino liquidity vault known for underlying {underlying_symbol!r}")

    async def _build_and_submit(
        self, authority: Pubkey, margin: Pubkey, *, spent: DiscoveredAsset, received: DiscoveredAsset,
        registry: AssetRegistry, cpi_accounts: list[AccountMeta], data: bytes, min_received: int,
        purpose: str, summary: dict,
    ) -> WriteEnvelope:
        instruction = await build_margin_execute_instruction(
            self._client, authority, margin,
            target_program=KLEND_PROGRAM_ID, spent=spent, received=received,
            cpi_accounts=cpi_accounts, data=data, min_received=min_received, registry=registry,
        )
        return await self._client.build_write_envelope(
            [instruction], authority, purpose=purpose, compute_unit_limit=600_000, summary=summary
        )
