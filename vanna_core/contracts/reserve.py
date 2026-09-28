"""Vanna Reserve contract wrapper — Earn (lender supply/redeem + pool stats).

Covers: lender_supply, lender_redeem, public_refresh_reserve, and the derived
pool-stats view (utilization, APY, exchange rate). Mirrors
VANNA_SOLANA_MCP_REQUIREMENTS.md Part B §3 (ReserveContract).

Instruction-building here assumes the lender's own token account (and, for
SOL, wrapped-SOL funding) already exists. Backend-Solana's frontend prepends
`createAssociatedTokenAccountIdempotentInstruction` (+ a SystemProgram
transfer / `createSyncNativeInstruction` for SOL) ahead of the Anchor
instruction on every deposit — lib/solana/protocol.ts
`ensureTokenAccountInstructions` / `ContractService.deposit`. That SPL-Token
prefix-instruction bundling is not yet ported here; the tool built on top of
this contract must add it before this is a genuinely turnkey deposit.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from solders.instruction import AccountMeta
from solders.pubkey import Pubkey

from vanna_core import math_utils
from vanna_core.client import SolanaClient
from vanna_core.layouts import Reserve
from vanna_core.pdas import asset_config_pda, protocol_config_pda, reserve_pda, share_mint_pda
from vanna_core.types import PoolStats, UnsignedTransaction

TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

LENDER_SUPPLY_DISC = bytes([52, 247, 77, 250, 74, 122, 100, 33])
LENDER_REDEEM_DISC = bytes([22, 48, 40, 70, 164, 48, 75, 224])
REFRESH_RESERVE_DISC = bytes([212, 53, 162, 184, 171, 222, 142, 156])


def _find_ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Standard SPL associated-token-account derivation (owner may be off-curve,
    e.g. a PDA — Anchor's ATA seeds don't distinguish the two cases)."""
    return Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


class ReserveContract:
    """One lending reserve (one asset: SOL or USDC)."""

    def __init__(self, client: SolanaClient, mint: Pubkey, symbol: str, decimals: int) -> None:
        self._client = client
        self.mint = mint
        self.symbol = symbol
        self.decimals = decimals
        self.reserve_address, _ = reserve_pda(mint, client.program_id)
        self.share_mint_address, _ = share_mint_pda(mint, client.program_id)
        self.asset_config_address, _ = asset_config_pda(mint, client.program_id)

    async def get_reserve(self):
        """Fetch + decode the raw Reserve account."""
        return await self._client.fetch_program_account(self.reserve_address, "Reserve", Reserve)

    async def get_share_supply(self) -> int:
        """Total supply of this reserve's share (v-token) SPL mint."""
        data = await self._client.fetch_raw_account(self.share_mint_address)
        # SPL Mint layout: mint_authority_option(4) mint_authority(32) supply(8) ...
        return int.from_bytes(data[36:44], "little")

    async def get_pool_stats(self) -> PoolStats:
        reserve, share_supply = await asyncio.gather(
            self.get_reserve(), self.get_share_supply()
        )
        liquidity = reserve.accounted_liquidity_assets
        borrowed = reserve.total_borrow_assets
        fees = reserve.accrued_protocol_fees
        total_assets = liquidity + borrowed - fees

        utilization = math_utils.utilization_rate_pct(liquidity, borrowed, fees)
        borrow_apr = math_utils.borrow_apr_pct(
            utilization,
            reserve.base_rate_bps,
            reserve.slope1_bps,
            reserve.slope2_bps,
            reserve.optimal_utilization_bps,
        )
        supply_apr = math_utils.supply_apr_pct(borrow_apr, utilization)

        return PoolStats(
            asset_symbol=self.symbol,
            reserve_address=str(self.reserve_address),
            total_supply=math_utils.ui_amount(total_assets, self.decimals),
            total_borrowed=math_utils.ui_amount(borrowed, self.decimals),
            available_liquidity=math_utils.ui_amount(max(0, liquidity - fees), self.decimals),
            utilization_rate_pct=str(utilization),
            supply_apy_pct=str(supply_apr),
            borrow_apy_pct=str(borrow_apr),
            exchange_rate=math_utils.exchange_rate(total_assets, share_supply),
            reserve_factor_bps=reserve.reserve_factor_bps,
            base_rate_bps=reserve.base_rate_bps,
            slope1_bps=reserve.slope1_bps,
            slope2_bps=reserve.slope2_bps,
            optimal_utilization_bps=reserve.optimal_utilization_bps,
        )

    async def get_deposited_balance(self, owner: Pubkey) -> str:
        """Lender's v-token (share) balance, human-readable."""
        share_account = _find_ata(owner, self.share_mint_address)
        try:
            data = await self._client.fetch_raw_account(share_account)
        except Exception:  # noqa: BLE001 — no share account yet == zero balance
            return "0"
        raw = int.from_bytes(data[64:72], "little")  # SPL TokenAccount.amount offset
        return math_utils.ui_amount(raw, self.decimals)

    async def build_supply_tx(
        self, lender: Pubkey, amount_ui: "Decimal | str", min_shares_out: int = 0
    ) -> UnsignedTransaction:
        """Build+simulate lender_supply. Caller's ATA for `mint` must already
        hold `amount_ui` (see module docstring re: prefix instructions)."""
        raw = math_utils.base_units(amount_ui, self.decimals)
        protocol_config, _ = protocol_config_pda(self._client.program_id)
        lender_token_account = _find_ata(lender, self.mint)
        liquidity_vault = _find_ata(self.reserve_address, self.mint)
        lender_share_account = _find_ata(lender, self.share_mint_address)

        accounts = [
            AccountMeta(lender, is_signer=True, is_writable=True),
            AccountMeta(protocol_config, is_signer=False, is_writable=False),
            AccountMeta(self.asset_config_address, is_signer=False, is_writable=False),
            AccountMeta(self.reserve_address, is_signer=False, is_writable=True),
            AccountMeta(self.mint, is_signer=False, is_writable=False),
            AccountMeta(lender_token_account, is_signer=False, is_writable=True),
            AccountMeta(liquidity_vault, is_signer=False, is_writable=True),
            AccountMeta(self.share_mint_address, is_signer=False, is_writable=True),
            AccountMeta(lender_share_account, is_signer=False, is_writable=True),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        args = _u64(raw) + _u64(min_shares_out)
        ix = self._client.build_instruction(LENDER_SUPPLY_DISC, args, accounts)
        return await self._client.simulate_and_build_unsigned([ix], lender, "lender_supply")

    async def build_redeem_tx(
        self, lender: Pubkey, shares_ui: "Decimal | str", min_assets_out: int = 0
    ) -> UnsignedTransaction:
        """Build+simulate lender_redeem."""
        raw_shares = math_utils.base_units(shares_ui, self.decimals)
        protocol_config, _ = protocol_config_pda(self._client.program_id)
        lender_token_account = _find_ata(lender, self.mint)
        liquidity_vault = _find_ata(self.reserve_address, self.mint)
        lender_share_account = _find_ata(lender, self.share_mint_address)

        accounts = [
            AccountMeta(lender, is_signer=True, is_writable=False),
            AccountMeta(protocol_config, is_signer=False, is_writable=False),
            AccountMeta(self.asset_config_address, is_signer=False, is_writable=False),
            AccountMeta(self.reserve_address, is_signer=False, is_writable=True),
            AccountMeta(self.mint, is_signer=False, is_writable=False),
            AccountMeta(lender_token_account, is_signer=False, is_writable=True),
            AccountMeta(liquidity_vault, is_signer=False, is_writable=True),
            AccountMeta(self.share_mint_address, is_signer=False, is_writable=True),
            AccountMeta(lender_share_account, is_signer=False, is_writable=True),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        args = _u64(raw_shares) + _u64(min_assets_out)
        ix = self._client.build_instruction(LENDER_REDEEM_DISC, args, accounts)
        return await self._client.simulate_and_build_unsigned([ix], lender, "lender_redeem")

    async def build_refresh_tx(self, fee_payer: Pubkey) -> UnsignedTransaction:
        """Permissionless interest-accrual tick. Any fee payer can call this."""
        accounts = [AccountMeta(self.reserve_address, is_signer=False, is_writable=True)]
        ix = self._client.build_instruction(REFRESH_RESERVE_DISC, b"", accounts)
        return await self._client.simulate_and_build_unsigned(
            [ix], fee_payer, "public_refresh_reserve"
        )
