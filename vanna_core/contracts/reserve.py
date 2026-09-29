"""Vanna Reserve contract wrapper — Earn (lender supply/redeem + pool stats).

Backs the `vanna_earn_market` / `vanna_earn_position` / `vanna_earn_write`
dispatchers (TOOLS_CATALOGUE.md §3.6-3.8). Covers `lender_supply`,
`lender_redeem`, and the derived pool-stats/deposited-balance views
(utilization, APR, exchange rate) — all math ported 1:1 from
`math/interest.rs` and `math/shares.rs` (verified against source,
2026-09-28) via `vanna_core.math_utils`.

`public_refresh_reserve` is deliberately not wrapped as a standalone method:
reads project accrual to the chain clock in memory (`get_pool_stats`,
`get_deposited_balance`) instead of needing a fresh on-chain tick first.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from solders.instruction import AccountMeta
from solders.pubkey import Pubkey

from vanna_core import math_utils
from vanna_core.assets import TOKEN_PROGRAM_ID
from vanna_core.client import SolanaClient
from vanna_core.envelope import WriteEnvelope
from vanna_core.layouts import Reserve as ReserveLayout
from vanna_core.pdas import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    asset_config_pda,
    find_associated_token_address,
    protocol_config_pda,
    reserve_pda,
)
from vanna_core.types import PoolStats
from vanna_core.wsol import wallet_prep_instructions

SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

LENDER_SUPPLY_DISC = bytes([52, 247, 77, 250, 74, 122, 100, 33])
LENDER_REDEEM_DISC = bytes([22, 48, 40, 70, 164, 48, 75, 224])

# Default slippage floor for supply/redeem min-out bounds (TOOLS_CATALOGUE.md §2.6).
DEFAULT_SLIPPAGE_BPS = 50  # 0.5%


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


def _apply_slippage_floor(expected: int, slippage_bps: int) -> int:
    return math_utils.mul_div_floor(expected, 10_000 - slippage_bps, 10_000)


class ReserveContract:
    """One lending reserve (one asset from the real registry, see assets.py)."""

    def __init__(self, client: SolanaClient, mint: Pubkey, symbol: str, decimals: int) -> None:
        self._client = client
        self.mint = mint
        self.symbol = symbol
        self.decimals = decimals
        self.reserve_address, _ = reserve_pda(mint, client.program_id)
        self.asset_config_address, _ = asset_config_pda(mint, client.program_id)

    async def get_reserve(self) -> ReserveLayout:
        """Fetch + decode the raw Reserve account."""
        return await self._client.fetch_program_account(self.reserve_address, "Reserve", ReserveLayout)

    async def get_share_supply(self, share_mint: Pubkey) -> int:
        """Total supply of this reserve's share (v-token) SPL mint."""
        data = await self._client.fetch_raw_account(share_mint)
        # SPL Mint layout: mint_authority_option(4) mint_authority(32) supply(8) ...
        return int.from_bytes(data[36:44], "little")

    async def _projected_state(self) -> tuple[ReserveLayout, int, math_utils.AccrualProjection]:
        """Reserve account, share supply, and interest projected to the chain
        clock — the shared basis for pool stats and deposited-balance reads."""
        reserve, now = await asyncio.gather(
            self.get_reserve(), self._client.get_chain_clock_unix_timestamp()
        )
        share_mint = Pubkey(bytes(reserve.share_mint))
        share_supply = await self.get_share_supply(share_mint)
        projection = math_utils.project_accrual(
            accounted_liquidity_assets=reserve.accounted_liquidity_assets,
            total_borrow_assets=reserve.total_borrow_assets,
            accrued_protocol_fees=reserve.accrued_protocol_fees,
            borrow_index_wad=reserve.borrow_index_wad,
            last_update_timestamp=reserve.last_update_timestamp,
            reserve_factor_bps=reserve.reserve_factor_bps,
            linear_coeff_wad=reserve.rate_curve.linear_coeff_wad,
            jump_coeff_wad=reserve.rate_curve.jump_coeff_wad,
            rate_multiplier_wad=reserve.rate_curve.rate_multiplier_wad,
            now_unix=now,
        )
        return reserve, share_supply, projection

    async def get_pool_stats(self) -> PoolStats:
        reserve, share_supply, projection = await self._projected_state()
        liquidity = reserve.accounted_liquidity_assets
        borrowed = projection.new_total_borrow_assets
        fees = projection.new_accrued_protocol_fees

        util_wad = math_utils.utilization_wad(liquidity, borrowed)
        rate_per_second = math_utils.borrow_rate_per_second_wad(
            reserve.rate_curve.linear_coeff_wad,
            reserve.rate_curve.jump_coeff_wad,
            reserve.rate_curve.rate_multiplier_wad,
            util_wad,
        )
        borrow_apr = math_utils.annual_rate_pct(rate_per_second)
        supply_apr = math_utils.supply_apr_pct(borrow_apr, util_wad, reserve.reserve_factor_bps)
        lender_assets = math_utils.lender_total_assets(liquidity, borrowed, fees)
        available = math_utils.available_lender_cash(liquidity, fees)

        return PoolStats(
            asset_symbol=self.symbol,
            reserve_address=str(self.reserve_address),
            total_supply=math_utils.ui_amount(max(0, lender_assets), self.decimals),
            total_borrowed=math_utils.ui_amount(borrowed, self.decimals),
            available_liquidity=math_utils.ui_amount(available, self.decimals),
            utilization_rate_pct=str((Decimal(util_wad) / Decimal(math_utils.WAD) * 100).quantize(Decimal("0.01"))),
            supply_apr_pct=str(supply_apr),
            borrow_apr_pct=str(borrow_apr),
            exchange_rate=self._exchange_rate_str(lender_assets, share_supply),
            reserve_factor_bps=reserve.reserve_factor_bps,
            supply_cap=math_utils.ui_amount(reserve.supply_cap, self.decimals) if reserve.supply_cap else "uncapped",
            borrow_cap=math_utils.ui_amount(reserve.borrow_cap, self.decimals) if reserve.borrow_cap else "uncapped",
            status=reserve.status,
            linear_coeff_wad=str(reserve.rate_curve.linear_coeff_wad),
            jump_coeff_wad=str(reserve.rate_curve.jump_coeff_wad),
            rate_multiplier_wad=str(reserve.rate_curve.rate_multiplier_wad),
        )

    @staticmethod
    def _exchange_rate_str(lender_assets: int, share_supply: int) -> str:
        if share_supply <= 0 or lender_assets <= 0:
            return "1"
        rate = Decimal(lender_assets) / Decimal(share_supply)
        return str(rate.quantize(Decimal("0.0000001")))

    async def get_deposited_balance(self, owner: Pubkey) -> dict[str, str]:
        """Lender's v-token (share) balance and redeemable underlying, both
        human-readable — TOOLS_CATALOGUE.md §3.7 `earn_position.deposited_balance`."""
        reserve, share_supply, projection = await self._projected_state()
        share_mint = Pubkey(bytes(reserve.share_mint))
        share_account = find_associated_token_address(owner, share_mint, TOKEN_PROGRAM_ID)
        try:
            data = await self._client.fetch_raw_account(share_account)
            raw_shares = int.from_bytes(data[64:72], "little")  # SPL TokenAccount.amount offset
        except Exception:  # noqa: BLE001 — no share account yet == zero balance
            raw_shares = 0

        lender_assets = math_utils.lender_total_assets(
            reserve.accounted_liquidity_assets,
            projection.new_total_borrow_assets,
            projection.new_accrued_protocol_fees,
        )
        redeemable_raw = math_utils.supply_shares_to_assets_down(
            raw_shares, share_supply, max(0, lender_assets)
        )
        return {
            "asset": self.symbol,
            "share_balance": math_utils.ui_amount(raw_shares, self.decimals),
            "redeemable_underlying": math_utils.ui_amount(redeemable_raw, self.decimals),
        }

    async def build_supply_tx(
        self,
        lender: Pubkey,
        amount_ui: "Decimal | str",
        *,
        min_shares_out: int | None = None,
        is_native: bool = False,
    ) -> WriteEnvelope:
        """Build+simulate lender_supply. Prepends the lender's wallet-side ATA
        creation (and SOL wrap, if `is_native`) — the program only
        auto-creates its own PDAs' token accounts, never the caller's."""
        from vanna_core.assets import assert_lend_borrow_offered

        assert_lend_borrow_offered(self.symbol, "supply")
        raw = math_utils.base_units(amount_ui, self.decimals)
        reserve = await self.get_reserve()
        share_mint = Pubkey(bytes(reserve.share_mint))
        liquidity_vault = Pubkey(bytes(reserve.liquidity_vault))

        if min_shares_out is None:
            _, share_supply, projection = await self._projected_state()
            lender_assets = math_utils.lender_total_assets(
                reserve.accounted_liquidity_assets,
                projection.new_total_borrow_assets,
                projection.new_accrued_protocol_fees,
            )
            expected = math_utils.assets_to_supply_shares_down(raw, share_supply, max(0, lender_assets))
            min_shares_out = _apply_slippage_floor(expected, DEFAULT_SLIPPAGE_BPS)

        protocol_config, _ = protocol_config_pda(self._client.program_id)
        lender_token_account = find_associated_token_address(lender, self.mint, TOKEN_PROGRAM_ID)
        lender_share_account = find_associated_token_address(lender, share_mint, TOKEN_PROGRAM_ID)

        prep = wallet_prep_instructions(
            payer=lender,
            owner=lender,
            mint=self.mint,
            token_program=TOKEN_PROGRAM_ID,
            is_native=is_native,
            wrap_lamports=raw if is_native else 0,
        )

        accounts = [
            AccountMeta(lender, is_signer=True, is_writable=True),
            AccountMeta(protocol_config, is_signer=False, is_writable=False),
            AccountMeta(self.asset_config_address, is_signer=False, is_writable=False),
            AccountMeta(self.reserve_address, is_signer=False, is_writable=True),
            AccountMeta(self.mint, is_signer=False, is_writable=False),
            AccountMeta(lender_token_account, is_signer=False, is_writable=True),
            AccountMeta(liquidity_vault, is_signer=False, is_writable=True),
            AccountMeta(share_mint, is_signer=False, is_writable=True),
            AccountMeta(lender_share_account, is_signer=False, is_writable=True),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),  # underlying's token_program
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),  # share_token_program: always classic SPL
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        args = _u64(raw) + _u64(min_shares_out)
        ix = self._client.build_instruction(LENDER_SUPPLY_DISC, args, accounts)
        return await self._client.build_write_envelope(
            [*prep, ix],
            lender,
            purpose="lender_supply",
            summary={"action": "supply", "asset": self.symbol, "amount": str(amount_ui)},
        )

    async def build_redeem_tx(
        self, lender: Pubkey, shares_ui: "Decimal | str", *, min_assets_out: int | None = None
    ) -> WriteEnvelope:
        """Build+simulate lender_redeem."""
        raw_shares = math_utils.base_units(shares_ui, self.decimals)
        reserve = await self.get_reserve()
        share_mint = Pubkey(bytes(reserve.share_mint))
        liquidity_vault = Pubkey(bytes(reserve.liquidity_vault))

        if min_assets_out is None:
            _, share_supply, projection = await self._projected_state()
            lender_assets = math_utils.lender_total_assets(
                reserve.accounted_liquidity_assets,
                projection.new_total_borrow_assets,
                projection.new_accrued_protocol_fees,
            )
            expected = math_utils.supply_shares_to_assets_down(raw_shares, share_supply, max(0, lender_assets))
            min_assets_out = _apply_slippage_floor(expected, DEFAULT_SLIPPAGE_BPS)

        protocol_config, _ = protocol_config_pda(self._client.program_id)
        lender_token_account = find_associated_token_address(lender, self.mint, TOKEN_PROGRAM_ID)
        lender_share_account = find_associated_token_address(lender, share_mint, TOKEN_PROGRAM_ID)

        accounts = [
            AccountMeta(lender, is_signer=True, is_writable=False),
            AccountMeta(protocol_config, is_signer=False, is_writable=False),
            AccountMeta(self.asset_config_address, is_signer=False, is_writable=False),
            AccountMeta(self.reserve_address, is_signer=False, is_writable=True),
            AccountMeta(self.mint, is_signer=False, is_writable=False),
            AccountMeta(lender_token_account, is_signer=False, is_writable=True),
            AccountMeta(liquidity_vault, is_signer=False, is_writable=True),
            AccountMeta(share_mint, is_signer=False, is_writable=True),
            AccountMeta(lender_share_account, is_signer=False, is_writable=True),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),  # underlying's token_program
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),  # share_token_program: always classic SPL
        ]
        args = _u64(raw_shares) + _u64(min_assets_out)
        ix = self._client.build_instruction(LENDER_REDEEM_DISC, args, accounts)
        return await self._client.build_write_envelope(
            [ix],
            lender,
            purpose="lender_redeem",
            summary={"action": "redeem", "asset": self.symbol, "shares": str(shares_ui)},
        )
