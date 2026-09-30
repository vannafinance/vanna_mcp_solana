"""Margin account lifecycle writes and collateral/borrow/repay writes.
Backs `vanna_account`'s write half and `vanna_margin_trade`
(TOOLS_CATALOGUE.md §3.3/§3.5). Every account list and discriminator here was
read directly from `vanna-credit-layer/programs/vanna_lending/src/
instructions/account_manager/{account,collateral,borrow}.rs` and cross-checked
against the real IDL — not assumed from a name list (a name list hides which
accounts share a token program; live simulation caught exactly that once).

Every health-checked write here (`borrow`, `withdraw_collateral`, and the
`deposit_and_borrow`/`close_position` composites that wrap them) calls the
`dev-fork` oracle adapter (`vanna_core/contracts/oracle_refresh.py`) first,
so a stale posted price is refreshed before the on-chain health check runs
rather than failing closed on every other attempt. If a price still can't be
resolved, the on-chain simulation fails closed with the program's own
stale-price error — it never fabricates or silently skips a price check.
"""

from __future__ import annotations

import asyncio

from solders.instruction import AccountMeta
from solders.pubkey import Pubkey

from vanna_core import math_utils
from vanna_core.assets import AssetRegistry, DiscoveredAsset, assert_lend_borrow_offered
from vanna_core.client import SolanaClient
from vanna_core.envelope import WriteEnvelope
from vanna_core.layouts import EMPTY_ASSET_INDEX
from vanna_core.layouts import MarginAccount as MarginAccountLayout
from vanna_core.pdas import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    debt_position_pda,
    find_associated_token_address,
    margin_pda,
    protocol_config_pda,
)
from vanna_core.remaining_accounts import build_health_accounts
from vanna_core.wsol import wallet_prep_instructions

SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

# Instruction discriminators — verified byte-for-byte against the real IDL's
# declared `discriminator` arrays.
DISC_CREATE_MARGIN = bytes([105, 23, 202, 134, 147, 166, 130, 205])
DISC_CLOSE_MARGIN = bytes([251, 12, 109, 24, 26, 96, 200, 131])
DISC_DEPOSIT_COLLATERAL = bytes([238, 206, 161, 246, 218, 26, 100, 157])
DISC_WITHDRAW_COLLATERAL = bytes([81, 34, 228, 68, 219, 88, 210, 28])
DISC_CLOSE_COLLATERAL_POSITION = bytes([131, 134, 11, 62, 44, 231, 4, 79])
DISC_OPEN_DEBT_POSITION = bytes([144, 183, 201, 223, 74, 100, 20, 205])
DISC_BORROW = bytes([125, 34, 221, 55, 252, 190, 132, 154])
DISC_CLOSE_DEBT_POSITION = bytes([127, 138, 97, 199, 18, 176, 126, 248])
DISC_REPAY_FROM_MARGIN = bytes([108, 103, 220, 205, 63, 240, 73, 147])
DISC_REPAY_FROM_WALLET = bytes([136, 29, 241, 215, 137, 62, 170, 22])

DEFAULT_SLIPPAGE_BPS = 50  # 0.5%, TOOLS_CATALOGUE.md §2.6


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


def _u128(value: int) -> bytes:
    return value.to_bytes(16, "little")


class MarginTradeContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client

    def margin_address(self, authority: Pubkey) -> Pubkey:
        return margin_pda(authority, self._client.program_id)[0]

    # -- account lifecycle ----------------------------------------------------

    async def create_margin(self, authority: Pubkey) -> WriteEnvelope:
        margin = self.margin_address(authority)
        accounts = [
            AccountMeta(authority, is_signer=True, is_writable=False),
            AccountMeta(authority, is_signer=True, is_writable=True),  # payer == authority
            AccountMeta(margin, is_signer=False, is_writable=True),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        ix = self._client.build_instruction(DISC_CREATE_MARGIN, b"", accounts)
        return await self._client.build_write_envelope(
            [ix], authority, purpose="user_create_margin",
            summary={"action": "create_margin", "margin_account": str(margin)},
        )

    async def close_margin(self, authority: Pubkey) -> WriteEnvelope:
        margin = self.margin_address(authority)
        accounts = [
            AccountMeta(authority, is_signer=True, is_writable=True),
            AccountMeta(margin, is_signer=False, is_writable=True),
        ]
        ix = self._client.build_instruction(DISC_CLOSE_MARGIN, b"", accounts)
        return await self._client.build_write_envelope(
            [ix], authority, purpose="user_close_margin", summary={"action": "close_margin"}
        )

    # -- collateral -------------------------------------------------------------

    def _deposit_collateral_instructions(self, authority: Pubkey, margin: Pubkey, asset: DiscoveredAsset, amount_ui) -> list:
        mint = Pubkey.from_string(asset.mint)
        token_program = Pubkey.from_string(asset.token_program)
        raw = math_utils.base_units(amount_ui, asset.decimals)

        protocol_config, _ = protocol_config_pda(self._client.program_id)
        source_token_account = find_associated_token_address(authority, mint, token_program)
        margin_vault = find_associated_token_address(margin, mint, token_program)

        from vanna_core.assets import ASSETS

        is_native = any(a.symbol == asset.symbol and a.is_native for a in ASSETS)
        prep = wallet_prep_instructions(
            payer=authority, owner=authority, mint=mint, token_program=token_program,
            is_native=is_native, wrap_lamports=raw if is_native else 0,
        )

        accounts = [
            AccountMeta(authority, is_signer=True, is_writable=True),
            AccountMeta(protocol_config, is_signer=False, is_writable=False),
            AccountMeta(margin, is_signer=False, is_writable=True),
            AccountMeta(Pubkey.from_string(asset.config_address), is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(source_token_account, is_signer=False, is_writable=True),
            AccountMeta(margin_vault, is_signer=False, is_writable=True),
            AccountMeta(token_program, is_signer=False, is_writable=False),
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        ix = self._client.build_instruction(DISC_DEPOSIT_COLLATERAL, _u64(raw), accounts)
        return [*prep, ix]

    async def deposit_collateral(self, authority: Pubkey, asset_symbol: str, amount_ui) -> WriteEnvelope:
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        margin = self.margin_address(authority)
        instructions = self._deposit_collateral_instructions(authority, margin, asset, amount_ui)
        return await self._client.build_write_envelope(
            instructions, authority, purpose="user_deposit_collateral",
            summary={"action": "deposit_collateral", "asset": asset.symbol, "amount": str(amount_ui)},
        )

    async def withdraw_collateral(
        self, authority: Pubkey, asset_symbol: str, amount_ui, min_health_factor_wad: int = 0
    ) -> WriteEnvelope:
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices

        await maybe_refresh_prices(self._client, self._client.settings)
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        margin = self.margin_address(authority)
        mint = Pubkey.from_string(asset.mint)
        token_program = Pubkey.from_string(asset.token_program)
        raw = math_utils.base_units(amount_ui, asset.decimals)

        margin_decoded = await self._client.fetch_program_account(margin, "MarginAccount", MarginAccountLayout)
        # With no debt the program allows any withdrawal and reads no prices, so it needs no
        # health accounts at all; with debt it needs every other position plus every oracle.
        if margin_decoded.debt_count == 0:
            remaining = []
        else:
            remaining = await build_health_accounts(
                self._client, margin, margin_decoded, registry,
                exclude_collateral_indexes={asset.asset_index}, exclude_debt_index=None,
                extra_assets=[asset],
            )

        protocol_config, _ = protocol_config_pda(self._client.program_id)
        destination_token_account = find_associated_token_address(authority, mint, token_program)
        margin_vault = find_associated_token_address(margin, mint, token_program)

        accounts = [
            AccountMeta(authority, is_signer=True, is_writable=True),
            AccountMeta(protocol_config, is_signer=False, is_writable=False),
            AccountMeta(margin, is_signer=False, is_writable=True),
            AccountMeta(Pubkey.from_string(asset.config_address), is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(destination_token_account, is_signer=False, is_writable=True),
            AccountMeta(margin_vault, is_signer=False, is_writable=True),
            AccountMeta(token_program, is_signer=False, is_writable=False),
            *remaining,
        ]
        args = _u64(raw) + _u128(min_health_factor_wad)
        ix = self._client.build_instruction(DISC_WITHDRAW_COLLATERAL, args, accounts)
        # The Vanna program only ever auto-creates its own PDAs' token
        # accounts, never the caller's wallet-side ATA (vanna_core/wsol.py) —
        # a withdrawal to a wallet that has never held this mint needs its
        # destination ATA created first.
        prep = wallet_prep_instructions(payer=authority, owner=authority, mint=mint, token_program=token_program)
        return await self._client.build_write_envelope(
            [*prep, ix], authority, purpose="user_withdraw_collateral", compute_unit_limit=600_000,
            summary={"action": "withdraw_collateral", "asset": asset.symbol, "amount": str(amount_ui)},
        )

    async def close_collateral_position(self, authority: Pubkey, asset_symbol: str) -> WriteEnvelope:
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        margin = self.margin_address(authority)
        mint = Pubkey.from_string(asset.mint)
        token_program = Pubkey.from_string(asset.token_program)
        margin_vault = find_associated_token_address(margin, mint, token_program)

        accounts = [
            AccountMeta(authority, is_signer=True, is_writable=True),
            AccountMeta(margin, is_signer=False, is_writable=False),
            AccountMeta(Pubkey.from_string(asset.config_address), is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(margin_vault, is_signer=False, is_writable=True),
            AccountMeta(token_program, is_signer=False, is_writable=False),
        ]
        ix = self._client.build_instruction(DISC_CLOSE_COLLATERAL_POSITION, b"", accounts)
        return await self._client.build_write_envelope(
            [ix], authority, purpose="user_close_collateral_position",
            summary={"action": "close_collateral_position", "asset": asset.symbol},
        )

    # -- borrow / debt ----------------------------------------------------------

    async def open_debt_position(self, authority: Pubkey, asset_symbol: str) -> WriteEnvelope:
        assert_lend_borrow_offered(asset_symbol, "open_debt")
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        margin = self.margin_address(authority)
        reserve = Pubkey.from_string(asset.reserve)
        debt_position, _ = debt_position_pda(margin, reserve, self._client.program_id)

        accounts = [
            AccountMeta(authority, is_signer=True, is_writable=False),
            AccountMeta(authority, is_signer=True, is_writable=True),  # payer == authority
            AccountMeta(margin, is_signer=False, is_writable=True),
            AccountMeta(Pubkey.from_string(asset.config_address), is_signer=False, is_writable=False),
            AccountMeta(reserve, is_signer=False, is_writable=False),
            AccountMeta(debt_position, is_signer=False, is_writable=True),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        ix = self._client.build_instruction(DISC_OPEN_DEBT_POSITION, b"", accounts)
        return await self._client.build_write_envelope(
            [ix], authority, purpose="user_open_debt_position",
            summary={"action": "open_debt_position", "asset": asset.symbol},
        )

    async def borrow(
        self, authority: Pubkey, asset_symbol: str, amount_ui, max_debt_shares: int | None = None,
        auto_open_debt_position: bool = True,
    ) -> WriteEnvelope:
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices

        assert_lend_borrow_offered(asset_symbol, "borrow")
        await maybe_refresh_prices(self._client, self._client.settings)
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        margin = self.margin_address(authority)
        margin_decoded = await self._client.fetch_program_account(margin, "MarginAccount", MarginAccountLayout)
        instructions = await self._borrow_instructions(
            authority, margin, asset, amount_ui, registry, margin_decoded,
            max_debt_shares=max_debt_shares, auto_open_debt_position=auto_open_debt_position,
        )
        return await self._client.build_write_envelope(
            instructions, authority, purpose="user_borrow", compute_unit_limit=600_000,
            summary={"action": "borrow", "asset": asset.symbol, "amount": str(amount_ui)},
        )

    async def _borrow_instructions(
        self, authority: Pubkey, margin: Pubkey, asset: DiscoveredAsset, amount_ui, registry: AssetRegistry,
        margin_decoded, *, max_debt_shares: int | None = None, auto_open_debt_position: bool = True,
    ) -> list:
        mint = Pubkey.from_string(asset.mint)
        token_program = Pubkey.from_string(asset.token_program)
        reserve_address = Pubkey.from_string(asset.reserve)
        debt_position, _ = debt_position_pda(margin, reserve_address, self._client.program_id)
        raw = math_utils.base_units(amount_ui, asset.decimals)

        from vanna_core.layouts import Reserve as ReserveLayout

        reserve, debt_position_exists = await asyncio.gather(
            self._client.fetch_program_account(reserve_address, "Reserve", ReserveLayout),
            self._client.account_exists(debt_position),
        )

        instructions = []
        if not debt_position_exists:
            if not auto_open_debt_position:
                raise ValueError(f"No open debt position for {asset.symbol}; call open_debt_position first")
            open_accounts = [
                AccountMeta(authority, is_signer=True, is_writable=False),
                AccountMeta(authority, is_signer=True, is_writable=True),
                AccountMeta(margin, is_signer=False, is_writable=True),
                AccountMeta(Pubkey.from_string(asset.config_address), is_signer=False, is_writable=False),
                AccountMeta(reserve_address, is_signer=False, is_writable=False),
                AccountMeta(debt_position, is_signer=False, is_writable=True),
                AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            ]
            instructions.append(self._client.build_instruction(DISC_OPEN_DEBT_POSITION, b"", open_accounts))

        remaining = await build_health_accounts(
            self._client, margin, margin_decoded, registry,
            exclude_collateral_indexes={asset.asset_index}, exclude_debt_index=asset.asset_index,
            extra_assets=[asset],
        )

        if max_debt_shares is None:
            expected = math_utils.assets_to_debt_shares_up(raw, reserve.total_borrow_shares, reserve.total_borrow_assets)
            max_debt_shares = expected + math_utils.mul_div_ceil(expected, DEFAULT_SLIPPAGE_BPS, 10_000) + 1

        protocol_config, _ = protocol_config_pda(self._client.program_id)
        reserve_vault = _pk(reserve.liquidity_vault)
        margin_vault = find_associated_token_address(margin, mint, token_program)

        accounts = [
            AccountMeta(authority, is_signer=True, is_writable=True),
            AccountMeta(protocol_config, is_signer=False, is_writable=False),
            AccountMeta(margin, is_signer=False, is_writable=True),
            AccountMeta(Pubkey.from_string(asset.config_address), is_signer=False, is_writable=False),
            AccountMeta(reserve_address, is_signer=False, is_writable=True),
            AccountMeta(debt_position, is_signer=False, is_writable=True),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(reserve_vault, is_signer=False, is_writable=True),
            AccountMeta(margin_vault, is_signer=False, is_writable=True),
            AccountMeta(token_program, is_signer=False, is_writable=False),
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            *remaining,
        ]
        args = _u64(raw) + _u128(max_debt_shares)
        instructions.append(self._client.build_instruction(DISC_BORROW, args, accounts))
        return instructions

    async def deposit_and_borrow(
        self, authority: Pubkey, deposit_asset_symbol: str, deposit_amount_ui,
        borrow_asset_symbol: str, borrow_amount_ui, max_debt_shares: int | None = None,
    ) -> WriteEnvelope:
        """Composite: deposit collateral and borrow (same or a different
        asset) in one transaction — replaces the on-chain program's removed
        `user_deposit_and_borrow` instruction. If `deposit_asset` isn't
        already active collateral, the borrow's `remaining_accounts` scan is
        built against a *projected* margin-account state (the deposit's
        effect on the active-slot arrays applied locally) — the deposit
        instruction activates that slot on-chain before the borrow
        instruction runs in the same transaction, so the off-chain scan must
        anticipate that or the transaction fails closed with
        `IncompletePositionAccounts`."""
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices

        assert_lend_borrow_offered(borrow_asset_symbol, "borrow")
        await maybe_refresh_prices(self._client, self._client.settings)
        registry = AssetRegistry(self._client)
        await registry.load()
        deposit_asset = registry.by_symbol(deposit_asset_symbol)
        borrow_asset = registry.by_symbol(borrow_asset_symbol)
        margin = self.margin_address(authority)
        margin_decoded = await self._client.fetch_program_account(margin, "MarginAccount", MarginAccountLayout)

        deposit_ixs = self._deposit_collateral_instructions(authority, margin, deposit_asset, deposit_amount_ui)

        was_active = deposit_asset.asset_index in margin_decoded.collateral_asset_indexes
        projected_indexes = list(margin_decoded.collateral_asset_indexes)
        if not was_active:
            for i, slot in enumerate(projected_indexes):
                if slot == EMPTY_ASSET_INDEX:
                    projected_indexes[i] = deposit_asset.asset_index
                    break
        projected_margin = _ProjectedMarginAccount(
            collateral_asset_indexes=projected_indexes,
            debt_asset_indexes=list(margin_decoded.debt_asset_indexes),
        )

        borrow_ixs = await self._borrow_instructions(
            authority, margin, borrow_asset, borrow_amount_ui, registry, projected_margin,
            max_debt_shares=max_debt_shares,
        )

        return await self._client.build_write_envelope(
            [*deposit_ixs, *borrow_ixs], authority, purpose="deposit_and_borrow", compute_unit_limit=600_000,
            summary={
                "action": "deposit_and_borrow", "deposit_asset": deposit_asset.symbol,
                "deposit_amount": str(deposit_amount_ui), "borrow_asset": borrow_asset.symbol,
                "borrow_amount": str(borrow_amount_ui), "atomic": True,
            },
        )

    async def close_position(self, authority: Pubkey) -> WriteEnvelope:
        """Composite: unwind everything on this margin account in the fewest
        agent-visible calls. Genuinely multi-step (TOOLS_CATALOGUE.md §3.14)
        — each call re-reads *real* current on-chain state (never a locally
        projected one) and returns the single next applicable transaction:
        repay all outstanding debt (one transaction, every repay is
        independent) -> close each zero-balance debt slot -> withdraw each
        collateral position one at a time (its own `remaining_accounts` scan
        is always built from real state, so no cross-step projection is
        needed) -> close each emptied collateral slot -> close the margin
        account once both counts are zero. Call this repeatedly until its
        `summary.next_step` is `"done"`.
        """
        from vanna_core.contracts.margin import MarginContract
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices

        margin = self.margin_address(authority)
        if not await self._client.account_exists(margin):
            # A prior call's close_margin already reclaimed this account's
            # rent — there is nothing left to unwind. This is the composite's
            # true terminal state, not an error.
            return WriteEnvelope(
                cluster=self._client.settings.solana_cluster_label,
                program_id=str(self._client.program_id),
                fee_payer=str(authority),
                transactions=[],
                summary={"action": "close_position", "next_step": "done"},
                signing_status="not_signable",
                simulation_success=True,
            )

        margin_contract = MarginContract(self._client)
        address, margin_decoded, collateral_rows, debt_rows, _snapshot = await margin_contract.snapshot(authority)
        registry = AssetRegistry(self._client)
        await registry.load()

        outstanding_debt = [r for r in debt_rows if r["raw_debt"] > 0]
        if outstanding_debt:
            await maybe_refresh_prices(self._client, self._client.settings)

            # `user_repay_from_margin(repay_all=true)` caps the transfer at
            # the margin vault's own balance (borrow.rs:395,
            # `target_repay = requested_gross.min(margin_vault.amount)`); a
            # vault holding only ever the exact principal borrowed can never
            # cover `debt_shares_to_assets_up`'s rounding-up plus interest
            # accrued since the borrow, so it fails closed with `ZeroAmount`
            # (6040) once the vault is drained to 0 on a second attempt
            # instead of silently under-repaying forever. Matches
            # `vanna-credit-backend/lib/solana/margin.ts`'s own
            # `REPAY_ALL_BUFFER_BPS = 20` (0.2%) pre-flight check — surface
            # the shortfall as a `needs_wallet_topup` step rather than
            # submitting a doomed transaction.
            shortfalls = []
            payable_rows = []
            for row in outstanding_debt:
                asset = registry.by_symbol(row["symbol"])
                margin_vault = find_associated_token_address(
                    address, Pubkey.from_string(asset.mint), Pubkey.from_string(asset.token_program)
                )
                vault_balance = await self._read_token_balance(margin_vault)
                required = row["raw_debt"] + (row["raw_debt"] * 20) // 10_000 + 1
                if vault_balance < required:
                    shortfalls.append({
                        "symbol": row["symbol"],
                        "vault_balance": vault_balance,
                        "required": required,
                        "shortfall_ui": math_utils.ui_amount(required - vault_balance, asset.decimals),
                    })
                else:
                    payable_rows.append(row)

            if shortfalls:
                return WriteEnvelope(
                    cluster=self._client.settings.solana_cluster_label,
                    program_id=str(self._client.program_id),
                    fee_payer=str(authority),
                    transactions=[],
                    summary={
                        "action": "close_position",
                        "next_step": "needs_wallet_topup",
                        "shortfalls": shortfalls,
                        "hint": (
                            "The margin vault can't cover debt + accrued interest for these "
                            "assets. Call vanna_margin_trade(action='repay_from_wallet', "
                            "repay_all=True) for each, then call close_position again."
                        ),
                    },
                    signing_status="not_signable",
                    simulation_success=False,
                )

            instructions = []
            for row in payable_rows:
                asset = registry.by_symbol(row["symbol"])
                reserve = Pubkey.from_string(asset.reserve)
                debt_position, _ = debt_position_pda(address, reserve, self._client.program_id)
                margin_vault = find_associated_token_address(address, Pubkey.from_string(asset.mint), Pubkey.from_string(asset.token_program))
                reserve_vault = await self._reserve_vault(reserve)
                accounts = [
                    AccountMeta(authority, is_signer=True, is_writable=False),
                    AccountMeta(address, is_signer=False, is_writable=True),
                    AccountMeta(Pubkey.from_string(asset.config_address), is_signer=False, is_writable=False),
                    AccountMeta(reserve, is_signer=False, is_writable=True),
                    AccountMeta(debt_position, is_signer=False, is_writable=True),
                    AccountMeta(Pubkey.from_string(asset.mint), is_signer=False, is_writable=False),
                    AccountMeta(margin_vault, is_signer=False, is_writable=True),
                    AccountMeta(reserve_vault, is_signer=False, is_writable=True),
                    AccountMeta(Pubkey.from_string(asset.token_program), is_signer=False, is_writable=False),
                ]
                args = _u64(0) + bytes([1])  # max_assets=0 (unused when repay_all), repay_all=True
                instructions.append(self._client.build_instruction(DISC_REPAY_FROM_MARGIN, args, accounts))
            return await self._client.build_write_envelope(
                instructions, authority, purpose="close_position:repay_all",
                summary={"action": "close_position", "next_step": "repay", "assets": [r["symbol"] for r in payable_rows]},
            )

        zero_debt_slots = [r for r in debt_rows if r["raw_debt"] == 0]
        if zero_debt_slots:
            envelope = await self.close_debt_position(authority, zero_debt_slots[0]["symbol"])
            envelope.summary["next_step"] = "close_debt"
            return envelope

        if collateral_rows:
            row = collateral_rows[0]
            balance_ui = math_utils.ui_amount(row["raw_balance"], registry.by_symbol(row["symbol"]).decimals)
            envelope = await self.withdraw_collateral(authority, row["symbol"], balance_ui)
            envelope.summary["next_step"] = "withdraw_collateral"
            return envelope

        if margin_decoded.collateral_count == 0 and margin_decoded.debt_count == 0:
            envelope = await self.close_margin(authority)
            envelope.summary["next_step"] = "close_margin"
            return envelope

        # Nothing left to do — no transaction to build (an empty-instruction
        # transaction isn't meaningful, and there's genuinely nothing more
        # to close: the margin account itself was already closed by a
        # previous call, or never had a debt/collateral position at all).
        from vanna_core.envelope import SimulationOutcome

        return WriteEnvelope(
            cluster=self._client.settings.solana_cluster_label,
            program_id=str(self._client.program_id),
            fee_payer=str(authority),
            transactions=[],
            summary={"action": "close_position", "next_step": "done"},
            signing_status="not_signable",
            simulation_success=True,
        )

    async def close_debt_position(self, authority: Pubkey, asset_symbol: str) -> WriteEnvelope:
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        margin = self.margin_address(authority)
        reserve = Pubkey.from_string(asset.reserve)
        debt_position, _ = debt_position_pda(margin, reserve, self._client.program_id)

        accounts = [
            AccountMeta(authority, is_signer=True, is_writable=True),
            AccountMeta(margin, is_signer=False, is_writable=False),
            AccountMeta(Pubkey.from_string(asset.config_address), is_signer=False, is_writable=False),
            AccountMeta(reserve, is_signer=False, is_writable=False),
            AccountMeta(debt_position, is_signer=False, is_writable=True),
        ]
        ix = self._client.build_instruction(DISC_CLOSE_DEBT_POSITION, b"", accounts)
        return await self._client.build_write_envelope(
            [ix], authority, purpose="user_close_debt_position",
            summary={"action": "close_debt_position", "asset": asset.symbol},
        )

    # -- repay --------------------------------------------------------------

    async def repay_from_margin(
        self, authority: Pubkey, asset_symbol: str, max_assets_ui, repay_all: bool = False
    ) -> WriteEnvelope:
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        margin = self.margin_address(authority)
        mint = Pubkey.from_string(asset.mint)
        token_program = Pubkey.from_string(asset.token_program)
        reserve = Pubkey.from_string(asset.reserve)
        debt_position, _ = debt_position_pda(margin, reserve, self._client.program_id)
        margin_vault = find_associated_token_address(margin, mint, token_program)
        reserve_vault = await self._reserve_vault(reserve)
        max_assets = 0 if repay_all else math_utils.base_units(max_assets_ui, asset.decimals)

        accounts = [
            AccountMeta(authority, is_signer=True, is_writable=False),
            AccountMeta(margin, is_signer=False, is_writable=True),
            AccountMeta(Pubkey.from_string(asset.config_address), is_signer=False, is_writable=False),
            AccountMeta(reserve, is_signer=False, is_writable=True),
            AccountMeta(debt_position, is_signer=False, is_writable=True),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(margin_vault, is_signer=False, is_writable=True),
            AccountMeta(reserve_vault, is_signer=False, is_writable=True),
            AccountMeta(token_program, is_signer=False, is_writable=False),
        ]
        args = _u64(max_assets) + bytes([1 if repay_all else 0])
        ix = self._client.build_instruction(DISC_REPAY_FROM_MARGIN, args, accounts)
        return await self._client.build_write_envelope(
            [ix], authority, purpose="user_repay_from_margin",
            summary={"action": "repay", "asset": asset.symbol, "repay_all": repay_all, "max_assets": str(max_assets_ui)},
        )

    async def repay_from_wallet(
        self, payer: Pubkey, margin_authority: Pubkey, asset_symbol: str, max_assets_ui, repay_all: bool = False
    ) -> WriteEnvelope:
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        margin = self.margin_address(margin_authority)
        mint = Pubkey.from_string(asset.mint)
        token_program = Pubkey.from_string(asset.token_program)
        reserve = Pubkey.from_string(asset.reserve)
        debt_position, _ = debt_position_pda(margin, reserve, self._client.program_id)
        payer_token_account = find_associated_token_address(payer, mint, token_program)
        reserve_vault = await self._reserve_vault(reserve)
        max_assets = 0 if repay_all else math_utils.base_units(max_assets_ui, asset.decimals)

        accounts = [
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(margin, is_signer=False, is_writable=True),
            AccountMeta(Pubkey.from_string(asset.config_address), is_signer=False, is_writable=False),
            AccountMeta(reserve, is_signer=False, is_writable=True),
            AccountMeta(debt_position, is_signer=False, is_writable=True),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(payer_token_account, is_signer=False, is_writable=True),
            AccountMeta(reserve_vault, is_signer=False, is_writable=True),
            AccountMeta(token_program, is_signer=False, is_writable=False),
        ]
        args = _u64(max_assets) + bytes([1 if repay_all else 0])
        ix = self._client.build_instruction(DISC_REPAY_FROM_WALLET, args, accounts)
        return await self._client.build_write_envelope(
            [ix], payer, purpose="public_repay_from_wallet",
            summary={
                "action": "repay_from_wallet", "asset": asset.symbol, "repay_all": repay_all,
                "max_assets": str(max_assets_ui), "margin_account": str(margin),
            },
        )

    # -- shared helpers -----------------------------------------------------

    async def _reserve_vault(self, reserve_address: Pubkey) -> Pubkey:
        from vanna_core.layouts import Reserve as ReserveLayout

        reserve = await self._client.fetch_program_account(reserve_address, "Reserve", ReserveLayout)
        return _pk(reserve.liquidity_vault)

    async def _read_token_balance(self, token_account: Pubkey) -> int:
        try:
            data = await self._client.fetch_raw_account(token_account)
        except Exception:  # noqa: BLE001 — no account yet == zero balance
            return 0
        return int.from_bytes(data[64:72], "little")



def _pk(raw: bytes) -> Pubkey:
    return Pubkey(bytes(raw))


class _ProjectedMarginAccount:
    """A locally-projected `MarginAccount` view — same two fields
    `build_health_accounts` reads, standing in for the real decoded
    account when a preceding instruction in the same transaction (e.g. a
    composite's own `deposit_collateral`) will have changed the active-slot
    arrays before the instruction being built actually executes on-chain."""

    __slots__ = ("collateral_asset_indexes", "debt_asset_indexes")

    def __init__(self, collateral_asset_indexes: list[int], debt_asset_indexes: list[int]) -> None:
        self.collateral_asset_indexes = collateral_asset_indexes
        self.debt_asset_indexes = debt_asset_indexes
