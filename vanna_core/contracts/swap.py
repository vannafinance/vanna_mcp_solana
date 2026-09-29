"""Jupiter swaps — margin-account (`margin_execute`) and plain wallet-to-wallet.
Backs `vanna_swap` (TOOLS_CATALOGUE.md §3.11). Verified against
`vanna-credit-backend/lib/solana/exec.ts::executeMarginSwap` and `swap.ts`'s
`executeWalletSwap` (2026-09-28) for the exact request shapes, and against
`adapters/jupiter.rs` for the two allowed selectors and the zero-platform-fee
requirement.

Any plain registered asset is swappable; Kamino receipts have no real market and
aren't offered here.
"""

from __future__ import annotations

import base64

from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from vanna_core import math_utils
from vanna_core.assets import JUPITER_V6_PROGRAM_ID, AssetRegistry
from vanna_core.client import SolanaClient
from vanna_core.contracts.jupiter import ALLOWED_SELECTORS, JupiterClient, decode_jupiter_instruction, fetch_lookup_tables
from vanna_core.envelope import SimulationOutcome, WriteEnvelope
from vanna_core.margin_execute import build_margin_execute_instruction
from vanna_core.pdas import find_associated_token_address, margin_pda
from vanna_core.types import ContractCallError

DEFAULT_SLIPPAGE_BPS = 100  # 1%, matches the app's own default


class SwapContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client
        self._jupiter = JupiterClient(client.settings)

    async def _pair(self, from_symbol: str, to_symbol: str, registry: AssetRegistry):
        from_asset = registry.by_symbol(from_symbol)
        to_asset = registry.by_symbol(to_symbol)
        if from_asset.symbol == to_asset.symbol:
            return None, None, "from and to must be different assets"
        if from_asset.is_receipt or to_asset.is_receipt:
            return None, None, "only plain (non-receipt) assets have a Jupiter market"
        return from_asset, to_asset, None

    async def quote(self, from_symbol: str, to_symbol: str, amount_ui, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
        registry = AssetRegistry(self._client)
        await registry.load()
        from_asset, to_asset, error = await self._pair(from_symbol, to_symbol, registry)
        if error:
            return {"error": "invalid_input", "message": error}

        raw_amount = math_utils.base_units(amount_ui, from_asset.decimals)
        quote = await self._jupiter.get_quote(from_asset.mint, to_asset.mint, raw_amount, slippage_bps)
        out_raw = int(quote["outAmount"])
        min_out_raw = int(quote.get("otherAmountThreshold", quote["outAmount"]))
        return {
            "from": from_asset.symbol,
            "to": to_asset.symbol,
            "in_amount": math_utils.ui_amount(raw_amount, from_asset.decimals),
            "out_amount": math_utils.ui_amount(out_raw, to_asset.decimals),
            "min_out_amount": math_utils.ui_amount(min_out_raw, to_asset.decimals),
            "price_impact_pct": quote.get("priceImpactPct"),
            "route": [leg.get("swapInfo", {}).get("label") for leg in quote.get("routePlan", [])],
            "slippage_bps": slippage_bps,
        }

    async def can_swap(self, authority: Pubkey, from_symbol: str, to_symbol: str, amount_ui) -> dict:
        from vanna_core.contracts.margin import MarginContract

        margin_contract = MarginContract(self._client)
        registry = AssetRegistry(self._client)
        await registry.load()
        from_asset, to_asset, error = await self._pair(from_symbol, to_symbol, registry)
        if error:
            return {"allowed": False, "reason": error}
        if not to_asset.collateral_enabled:
            return {"allowed": False, "reason": f"{to_asset.symbol} is not collateral_enabled"}

        address, margin, collateral_rows, debt_rows, current = await margin_contract.snapshot(authority)
        from_row = next((r for r in collateral_rows if r["symbol"] == from_asset.symbol), None)
        if from_row is None:
            return {"allowed": False, "reason": f"no active {from_asset.symbol} collateral position"}

        raw_amount = math_utils.base_units(amount_ui, from_asset.decimals)
        if raw_amount > from_row["raw_balance"]:
            return {"allowed": False, "reason": "amount exceeds vault balance"}

        quote = await self._jupiter.get_quote(from_asset.mint, to_asset.mint, raw_amount)
        min_out_raw = int(quote.get("otherAmountThreshold", quote["outAmount"]))

        from vanna_core import math_utils as mu

        spent_usd_removed = mu.mul_div_floor(from_row["usd_value_nano"], raw_amount, from_row["raw_balance"]) if from_row["raw_balance"] else 0
        # Received leg valued at the quote's guaranteed minimum, conservatively.
        to_price, _view = await margin_contract.asset_price(to_asset)
        received_usd = to_price.value_of(min_out_raw, to_asset.decimals, False)

        # While in debt the program demands every price check on every asset of the account
        # after the call (`margin_execute` -> `PriceStatus::ALL_CHECKS`); fail closed here too.
        if debt_rows:
            from vanna_core.pricing import ALL_CHECKS, OraclePrice, PriceUnavailableError

            status = to_price.status
            for row in (*collateral_rows, *debt_rows):
                status &= row['price_status_bits']
            try:
                OraclePrice(to_price.price, to_price.receipt_rate, to_price.timestamp, status).require(ALL_CHECKS)
            except PriceUnavailableError as exc:
                return {'allowed': False, 'reason': str(exc)}

        new_collateral_values = [
            (r["usd_value_nano"] - spent_usd_removed) if r["symbol"] == from_asset.symbol else r["usd_value_nano"]
            for r in collateral_rows
        ]
        if to_asset.symbol not in {r["symbol"] for r in collateral_rows}:
            new_collateral_values.append(received_usd)
        else:
            new_collateral_values = [
                v + received_usd if r["symbol"] == to_asset.symbol else v
                for r, v in zip(collateral_rows, new_collateral_values, strict=True)
            ]
        projected = mu.calculate_health(new_collateral_values, [r["usd_value_nano"] for r in debt_rows])

        return {
            "allowed": projected.is_borrow_healthy,
            "reason": None if projected.is_borrow_healthy else "would breach the 1.10 health-factor threshold",
            "current_health_factor": current.health_factor_str,
            "projected_health_factor": projected.health_factor_str,
            "quoted_min_out": math_utils.ui_amount(min_out_raw, to_asset.decimals),
        }

    async def margin_swap(self, authority: Pubkey, from_symbol: str, to_symbol: str, amount_ui, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> WriteEnvelope:
        from vanna_core.contracts.oracle_refresh import maybe_refresh_prices

        await maybe_refresh_prices(self._client, self._client.settings)
        registry = AssetRegistry(self._client)
        await registry.load()
        from_asset, to_asset, error = await self._pair(from_symbol, to_symbol, registry)
        if error:
            raise ValueError(error)

        margin = margin_pda(authority, self._client.program_id)[0]
        raw_amount = math_utils.base_units(amount_ui, from_asset.decimals)
        quote = await self._jupiter.get_quote(from_asset.mint, to_asset.mint, raw_amount, slippage_bps)
        swap_response = await self._jupiter.get_swap_instructions(
            quote, str(margin), wrap_and_unwrap_sol=False, use_shared_accounts=True
        )
        swap_ix_dict = swap_response["swapInstruction"]
        if swap_ix_dict["programId"] != str(JUPITER_V6_PROGRAM_ID):
            raise ContractCallError(f"Unexpected swap program {swap_ix_dict['programId']}")
        raw_data = decode_jupiter_instruction(swap_ix_dict).data
        if raw_data[:8] not in ALLOWED_SELECTORS:
            raise ContractCallError("Jupiter returned a route type the margin adapter doesn't allow")

        cpi_instruction = decode_jupiter_instruction(swap_ix_dict)
        min_received = int(quote.get("otherAmountThreshold", quote["outAmount"]))

        instruction = await build_margin_execute_instruction(
            self._client, authority, margin,
            target_program=JUPITER_V6_PROGRAM_ID, spent=from_asset, received=to_asset,
            cpi_accounts=cpi_instruction.accounts, data=cpi_instruction.data,
            min_received=min_received, registry=registry,
        )

        lookup_tables = await fetch_lookup_tables(self._client, swap_response.get("addressLookupTableAddresses", []))
        return await self._client.build_write_envelope(
            [instruction], authority, purpose="margin_execute:jupiter_swap", compute_unit_limit=1_000_000,
            address_lookup_tables=lookup_tables,
            summary={"action": "margin_swap", "from": from_asset.symbol, "to": to_asset.symbol, "amount": str(amount_ui)},
        )

    async def wallet_swap(self, owner: Pubkey, from_symbol: str, to_symbol: str, amount_ui, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> WriteEnvelope:
        registry = AssetRegistry(self._client)
        await registry.load()
        from_asset, to_asset, error = await self._pair(from_symbol, to_symbol, registry)
        if error:
            raise ValueError(error)

        raw_amount = math_utils.base_units(amount_ui, from_asset.decimals)
        quote = await self._jupiter.get_quote(from_asset.mint, to_asset.mint, raw_amount, slippage_bps)
        swap_response = await self._jupiter.get_swap_transaction(quote, str(owner))

        raw_tx = base64.b64decode(swap_response["swapTransaction"])
        jupiter_tx = VersionedTransaction.from_bytes(raw_tx)

        # Jupiter builds against mainnet; this fork has its own blockhash
        # queue, so the message is rebuilt with ours — everything else
        # (accounts, instructions, lookup tables) comes from Jupiter as-is.
        blockhash, last_valid_block_height = await self._client.get_latest_blockhash()
        message = jupiter_tx.message
        rebuilt_message = MessageV0(
            message.header, message.account_keys, blockhash, message.instructions, message.address_table_lookups
        )
        placeholder_sigs = [Signature.default() for _ in range(rebuilt_message.header.num_required_signatures)]
        unsigned_tx = VersionedTransaction.populate(rebuilt_message, placeholder_sigs)

        sim = await self._client.simulate_versioned_transaction(unsigned_tx)
        success = sim.value.err is None
        error_str = None
        if not success:
            logs_tail = "\n".join((sim.value.logs or [])[-6:])
            error_str = f"{sim.value.err}\n{logs_tail}"

        simulation = SimulationOutcome(
            status="success" if success else "failed",
            err=error_str,
            logs=list(sim.value.logs or []),
            units_consumed=sim.value.units_consumed,
            context_slot=sim.context.slot if sim.context else None,
        )
        return WriteEnvelope.from_single_transaction(
            cluster=self._client.settings.solana_cluster_label,
            program_id=str(JUPITER_V6_PROGRAM_ID),
            fee_payer=str(owner),
            purpose="jupiter_wallet_swap",
            unsigned_tx_b64=base64.b64encode(bytes(unsigned_tx)).decode(),
            blockhash=str(blockhash),
            last_valid_block_height=last_valid_block_height,
            required_signers=[str(owner)],
            simulation=simulation,
            summary={"action": "wallet_swap", "from": from_asset.symbol, "to": to_asset.symbol, "amount": str(amount_ui)},
        )
