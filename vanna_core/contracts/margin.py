"""Margin account reads: resolve/get/health (`vanna_account`, TOOLS_CATALOGUE.md
3.3) and the position-level reads `vanna_margin_status` builds on (3.4).

Health valuation mirrors the program exactly: `math/health.rs::calculate_health`
(vanna_core.math_utils) over prices from the oracle facade (vanna_core.pricing,
a port of `oracle/*.rs`). USD values are nano-USD, collateral rounds down, debt
rounds up, one account-wide 1.10 threshold. Every asset is priced from its own
on-chain `OracleConfig` (Scope chain, Pyth fallback, klend receipt rate); a
position that can't be priced makes the whole read fail closed
(`PriceUnavailableError`) - never a partial or silently-skipped position.
Per-price quality flags (fresh / TWAP / confidence) are reported, and the write
previews demand the same checks the program demands (`PriceStatus::ALL_CHECKS`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from solders.pubkey import Pubkey

from vanna_core import math_utils
from vanna_core.assets import AssetRegistry, DiscoveredAsset, assert_lend_borrow_offered
from vanna_core.client import SolanaClient
from vanna_core.layouts import EMPTY_ASSET_INDEX, RESERVE_STATUS_ACTIVE
from vanna_core.layouts import DebtPosition as DebtPositionLayout
from vanna_core.layouts import MarginAccount as MarginAccountLayout
from vanna_core.layouts import Reserve as ReserveLayout
from vanna_core.oracle_reader import OracleReader, OracleView, describe_price
from vanna_core.pdas import debt_position_pda, find_associated_token_address, margin_pda
from vanna_core.pricing import ALL_CHECKS, LIQUIDATION_CHECKS, OraclePrice, PriceUnavailableError

# The program's own `MarginAccount.status` field is currently always 0 (Active);
# decode it opaquely rather than assume.
_MARGIN_STATUS_NAMES: dict[int, str] = {0: "Active"}

_INTERNAL_KEYS = {"usd_value_nano", "price_status_bits"}


@dataclass
class _Snapshot:
    address: Pubkey
    margin: object
    registry: AssetRegistry
    collateral_rows: list[dict]
    debt_rows: list[dict]
    health: math_utils.HealthSnapshot
    view: OracleView

    @property
    def price_status(self) -> int:
        status = ALL_CHECKS
        for row in (*self.collateral_rows, *self.debt_rows):
            status &= row["price_status_bits"]
        return status


class MarginContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client
        self._reader = OracleReader(client)

    def margin_address(self, authority: Pubkey) -> Pubkey:
        address, _ = margin_pda(authority, self._client.program_id)
        return address

    async def resolve(self, authority: Pubkey) -> dict:
        address = self.margin_address(authority)
        exists = await self._client.account_exists(address)
        return {"exists": exists, "margin_account": str(address), "authority": str(authority)}

    async def get(self, authority: Pubkey) -> dict:
        address = self.margin_address(authority)
        margin = await self._client.fetch_program_account(address, "MarginAccount", MarginAccountLayout)
        registry = AssetRegistry(self._client)
        await registry.load()

        collateral = await self._resolve_slots(margin.collateral_asset_indexes, registry)
        debt = await self._resolve_slots(margin.debt_asset_indexes, registry)

        return {
            "margin_account": str(address),
            "authority": str(authority),
            "status": _MARGIN_STATUS_NAMES.get(margin.status, "Unknown"),
            "collateral_count": margin.collateral_count,
            "debt_count": margin.debt_count,
            "collateral_assets": [d.symbol for d in collateral],
            "debt_assets": [d.symbol for d in debt],
            "event_sequence": margin.event_sequence,
        }

    @staticmethod
    async def _resolve_slots(indexes, registry: AssetRegistry) -> list[DiscoveredAsset]:
        active = [i for i in indexes if i != EMPTY_ASSET_INDEX]
        return await asyncio.gather(*(registry.by_index(i) for i in active))

    async def health(self, authority: Pubkey) -> dict:
        snap = await self._snapshot(authority)
        health = snap.health
        return {
            "margin_account": str(snap.address),
            "collateral": [_strip_internal(r) for r in snap.collateral_rows],
            "debt": [_strip_internal(r) for r in snap.debt_rows],
            "total_collateral_usd": math_utils.usd_value_ui(health.total_collateral_value),
            "total_debt_usd": math_utils.usd_value_ui(health.total_debt_value),
            "health_factor": health.health_factor_str,
            "is_healthy": health.is_borrow_healthy,
            "is_liquidatable": health.is_liquidatable,
            "price_checks_pass": snap.price_status == ALL_CHECKS,
        }

    async def list_collateral(self, authority: Pubkey) -> dict:
        snap = await self._snapshot(authority)
        return {"margin_account": str(snap.address), "collateral": [_strip_internal(r) for r in snap.collateral_rows]}

    async def list_debt(self, authority: Pubkey) -> dict:
        snap = await self._snapshot(authority)
        return {"margin_account": str(snap.address), "debt": [_strip_internal(r) for r in snap.debt_rows]}

    async def liquidation_snapshot(self, authority: Pubkey) -> dict:
        """Eligibility read for the program's whole-account liquidation: at HF <= 1.10 a
        liquidator repays EVERY debt in full and receives EVERY collateral balance (no close
        factor, no bonus, no health-improvement check); their reward is whatever the collateral
        is worth beyond the debt. Only a fresh price is required (a TWAP or confidence alarm
        never blocks a liquidation)."""
        snap = await self._snapshot(authority)
        health = snap.health
        fresh = bool(snap.price_status & LIQUIDATION_CHECKS)
        return {
            "margin_account": str(snap.address),
            "liquidation_model": "whole_account",
            "is_liquidatable": health.is_liquidatable and fresh,
            "health_at_or_below_threshold": health.is_liquidatable,
            "prices_fresh": fresh,
            "health_factor": health.health_factor_str,
            "total_collateral_usd": math_utils.usd_value_ui(health.total_collateral_value),
            "total_debt_usd": math_utils.usd_value_ui(health.total_debt_value),
            "estimated_reward_usd": _signed_usd(health.total_collateral_value - health.total_debt_value),
            "debt_to_repay": [_strip_internal(r) for r in snap.debt_rows],
            "collateral_to_seize": [_strip_internal(r) for r in snap.collateral_rows],
        }

    async def asset_price(self, asset: DiscoveredAsset) -> tuple[OraclePrice, OracleView]:
        """One asset's price (and the view it was read from), read the way the program will."""
        return await self._reader.price(asset)

    # -- previews (mirror the program's own gates, fail closed) ------------------------------------

    async def preview_borrow(self, authority: Pubkey, asset_symbol: str, amount_ui: "Decimal | str") -> dict:
        assert_lend_borrow_offered(asset_symbol, "borrow")
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        snap = await self._snapshot(authority, registry=registry, extra_assets=[asset])
        current = snap.health

        if not asset.borrow_enabled:
            return {"allowed": False, "reason": f"{asset.symbol} is not borrow_enabled"}

        reserve = await self._client.fetch_program_account(Pubkey.from_string(asset.reserve), "Reserve", ReserveLayout)
        protocol = await self._get_protocol_config()
        if protocol["operating_mode_code"] != 0:
            return {"allowed": False, "reason": f"operating_mode={protocol['operating_mode']} blocks borrow"}
        if reserve.status != RESERVE_STATUS_ACTIVE:
            return {"allowed": False, "reason": f"reserve.status={reserve.status} blocks borrow (must be Active)"}

        raw_amount = math_utils.base_units(amount_ui, asset.decimals)
        if reserve.borrow_cap and reserve.total_borrow_assets + raw_amount > reserve.borrow_cap:
            return {"allowed": False, "reason": "borrow_cap exceeded"}
        if raw_amount > reserve.accounted_liquidity_assets:
            return {"allowed": False, "reason": "insufficient pool liquidity"}

        margin = snap.margin
        opens_debt_slot = asset.symbol not in {r["symbol"] for r in snap.debt_rows}
        opens_collateral_slot = asset.collateral_enabled and asset.symbol not in {r["symbol"] for r in snap.collateral_rows}
        new_slots = int(opens_debt_slot) + int(opens_collateral_slot)
        if new_slots and margin.collateral_count + margin.debt_count + new_slots > protocol["max_assets_per_margin"]:
            return {"allowed": False, "reason": "margin account slot limit reached"}

        price = snap.view.price(asset)
        # Borrowing needs every price check on every asset of the account (`PriceStatus::ALL_CHECKS`).
        try:
            OraclePrice(price.price, price.receipt_rate, price.timestamp, price.status & snap.price_status).require(ALL_CHECKS)
        except PriceUnavailableError as exc:
            return {"allowed": False, "reason": str(exc)}

        existing_debt = next((r for r in snap.debt_rows if r["symbol"] == asset.symbol), None)
        new_debt_values = [r["usd_value_nano"] for r in snap.debt_rows if r["symbol"] != asset.symbol]
        new_debt_raw = (existing_debt["raw_debt"] if existing_debt else 0) + raw_amount
        new_debt_values.append(price.value_of(new_debt_raw, asset.decimals, True))

        new_collateral_values = [r["usd_value_nano"] for r in snap.collateral_rows if r["symbol"] != asset.symbol or not asset.collateral_enabled]
        if asset.collateral_enabled:
            existing_balance = next((r["raw_balance"] for r in snap.collateral_rows if r["symbol"] == asset.symbol), 0)
            new_collateral_values.append(price.value_of(existing_balance + raw_amount, asset.decimals, False))
        projected = math_utils.calculate_health(new_collateral_values, new_debt_values)

        return {
            "allowed": projected.is_borrow_healthy,
            "reason": None if projected.is_borrow_healthy else "would breach the 1.10 health-factor threshold",
            "current_health_factor": current.health_factor_str,
            "projected_health_factor": projected.health_factor_str,
        }

    async def preview_withdraw(self, authority: Pubkey, asset_symbol: str, amount_ui: "Decimal | str") -> dict:
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol, allow_unoffered=True)
        snap = await self._snapshot(authority, registry=registry, extra_assets=[asset])
        current = snap.health

        row = next((r for r in snap.collateral_rows if r["symbol"] == asset.symbol), None)
        if row is None:
            return {"allowed": False, "reason": f"no active {asset.symbol} collateral position"}

        raw_amount = math_utils.base_units(amount_ui, asset.decimals)
        if raw_amount > row["raw_balance"]:
            return {"allowed": False, "reason": "amount exceeds vault balance"}

        protocol = await self._get_protocol_config()
        if protocol["operating_mode_code"] == 3:  # Halted
            return {"allowed": False, "reason": "operating_mode=Halted blocks withdraw"}

        # With no debt a withdrawal can't hurt the protocol: the program reads no prices and allows it.
        if snap.margin.debt_count == 0:
            return {
                "allowed": True,
                "reason": None,
                "current_health_factor": current.health_factor_str,
                "projected_health_factor": current.health_factor_str,
            }

        price = snap.view.price(asset)
        try:
            OraclePrice(price.price, price.receipt_rate, price.timestamp, price.status & snap.price_status).require(ALL_CHECKS)
        except PriceUnavailableError as exc:
            return {"allowed": False, "reason": str(exc)}

        remaining_value = price.value_of(row["raw_balance"] - raw_amount, asset.decimals, False)
        new_collateral_values = [
            remaining_value if r["symbol"] == asset.symbol else r["usd_value_nano"] for r in snap.collateral_rows
        ]
        projected = math_utils.calculate_health(new_collateral_values, [r["usd_value_nano"] for r in snap.debt_rows])

        return {
            "allowed": projected.is_borrow_healthy,
            "reason": None if projected.is_borrow_healthy else "would breach the 1.10 health-factor threshold",
            "current_health_factor": current.health_factor_str,
            "projected_health_factor": projected.health_factor_str,
        }

    async def max_borrow(self, authority: Pubkey, asset_symbol: str) -> dict:
        """Largest amount of `asset` that `preview_borrow` will accept: the closed-form health
        headroom, capped by pool liquidity and the borrow cap, then verified (and stepped down)
        through `preview_borrow` itself, so it can never exceed what a real borrow allows."""
        assert_lend_borrow_offered(asset_symbol, "borrow")
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        snap = await self._snapshot(authority, registry=registry, extra_assets=[asset])

        zero = {"asset": asset.symbol, "max_amount": "0", "raw_max_amount": 0}
        # Probe the non-amount gates (mode, status, caps, slots) with the smallest borrowable unit;
        # a zero amount is rejected by the amount validator, not by a gate.
        first = await self.preview_borrow(authority, asset.symbol, math_utils.ui_amount(1, asset.decimals))
        blocking = first["reason"]
        if blocking and "would breach" not in blocking and blocking != "insufficient pool liquidity":
            return {**zero, "reason": blocking}

        collateral = snap.health.total_collateral_value
        debt = snap.health.total_debt_value
        # (C + x) / (D + x) > 1.10  =>  x < 10*C - 11*D   (borrowed funds count as collateral)
        # C / (D + x) > 1.10        =>  x < C/1.1 - D     (asset is not collateral-enabled)
        headroom_usd = 10 * collateral - 11 * debt if asset.collateral_enabled else collateral * 10 // 11 - debt
        if headroom_usd <= 0:
            return {**zero, "reason": "no borrowing headroom at the 1.10 health-factor threshold"}

        reserve = await self._client.fetch_program_account(Pubkey.from_string(asset.reserve), "Reserve", ReserveLayout)
        price = snap.view.price(asset)
        raw = _usd_to_raw(price, headroom_usd, asset.decimals)
        raw = min(raw, reserve.accounted_liquidity_assets)
        if reserve.borrow_cap:
            raw = min(raw, max(0, reserve.borrow_cap - reserve.total_borrow_assets))

        for _ in range(12):
            if raw <= 0:
                break
            check = await self.preview_borrow(authority, asset.symbol, math_utils.ui_amount(raw, asset.decimals))
            if check["allowed"]:
                return {
                    "asset": asset.symbol,
                    "max_amount": math_utils.ui_amount(raw, asset.decimals),
                    "raw_max_amount": raw,
                    "reason": None,
                    "current_health_factor": snap.health.health_factor_str,
                }
            raw = raw - max(1, raw // 1000)  # step down 0.1% (strict-inequality / rounding edge)
        return {**zero, "reason": "no borrowable amount passes the health check"}

    async def max_withdraw(self, authority: Pubkey, asset_symbol: str) -> dict:
        """Largest amount of `asset` collateral withdrawable while staying above the 1.10
        threshold (verified through `preview_withdraw`)."""
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol, allow_unoffered=True)
        snap = await self._snapshot(authority, registry=registry, extra_assets=[asset])
        row = next((r for r in snap.collateral_rows if r["symbol"] == asset.symbol), None)
        zero = {"asset": asset.symbol, "max_amount": "0", "raw_max_amount": 0}
        if row is None:
            return {**zero, "reason": f"no active {asset.symbol} collateral position"}

        raw = row["raw_balance"]
        debt = snap.health.total_debt_value
        if snap.margin.debt_count > 0:
            headroom_usd = snap.health.total_collateral_value - (11 * debt) // 10  # C - w > 1.1 * D
            if headroom_usd <= 0:
                return {**zero, "reason": "no withdrawable headroom at the 1.10 health-factor threshold"}
            price = snap.view.price(asset)
            raw = min(raw, _usd_to_raw(price, headroom_usd, asset.decimals))

        for _ in range(12):
            if raw <= 0:
                break
            check = await self.preview_withdraw(authority, asset.symbol, math_utils.ui_amount(raw, asset.decimals))
            if check["allowed"]:
                return {
                    "asset": asset.symbol,
                    "max_amount": math_utils.ui_amount(raw, asset.decimals),
                    "raw_max_amount": raw,
                    "reason": None,
                    "current_health_factor": snap.health.health_factor_str,
                }
            raw = raw - max(1, raw // 1000)
        return {**zero, "reason": "no withdrawable amount passes the health check"}

    async def _get_protocol_config(self) -> dict:
        from vanna_core.contracts.protocol import ProtocolContract

        return await ProtocolContract(self._client).get_config()

    # -- snapshot ------------------------------------------------------------------------------

    async def snapshot(self, authority: Pubkey):
        """(address, margin, collateral_rows, debt_rows, health) - kept for callers that predate
        `_snapshot` (swap.can_swap, margin_trade.close_position)."""
        snap = await self._snapshot(authority)
        return snap.address, snap.margin, snap.collateral_rows, snap.debt_rows, snap.health

    async def _snapshot(
        self,
        authority: Pubkey,
        *,
        registry: AssetRegistry | None = None,
        extra_assets: list[DiscoveredAsset] | None = None,
    ) -> _Snapshot:
        address = self.margin_address(authority)
        margin = await self._client.fetch_program_account(address, "MarginAccount", MarginAccountLayout)
        if registry is None:
            registry = AssetRegistry(self._client)
            await registry.load()

        collateral_assets = await self._resolve_slots(margin.collateral_asset_indexes, registry)
        debt_assets = await self._resolve_slots(margin.debt_asset_indexes, registry)
        view = await self._reader.view([*collateral_assets, *debt_assets, *(extra_assets or [])])

        collateral_rows, debt_rows = await asyncio.gather(
            asyncio.gather(*(self._value_collateral(address, a, view) for a in collateral_assets)),
            asyncio.gather(*(self._value_debt(address, a, view) for a in debt_assets)),
        )
        health = math_utils.calculate_health(
            [row["usd_value_nano"] for row in collateral_rows],
            [row["usd_value_nano"] for row in debt_rows],
        )
        return _Snapshot(address, margin, registry, list(collateral_rows), list(debt_rows), health, view)

    async def _value_collateral(self, margin_address: Pubkey, asset: DiscoveredAsset, view: OracleView) -> dict:
        token_program = Pubkey.from_string(asset.token_program)
        vault = find_associated_token_address(margin_address, Pubkey.from_string(asset.mint), token_program)
        balance_raw = await self._read_token_balance(vault)
        price = view.price(asset)
        usd_value = price.value_of(balance_raw, asset.decimals, False)
        return {
            "symbol": asset.symbol,
            "raw_balance": balance_raw,
            "ui_balance": math_utils.ui_amount(balance_raw, asset.decimals),
            "usd_value": math_utils.usd_value_ui(usd_value),
            "price": describe_price(price, view.now),
            "usd_value_nano": usd_value,
            "price_status_bits": price.status,
        }

    async def _value_debt(self, margin_address: Pubkey, asset: DiscoveredAsset, view: OracleView) -> dict:
        reserve_address = Pubkey.from_string(asset.reserve)
        debt_position_address, _ = debt_position_pda(margin_address, reserve_address, self._client.program_id)
        reserve, debt_position = await asyncio.gather(
            self._client.fetch_program_account(reserve_address, "Reserve", ReserveLayout),
            self._client.fetch_program_account(debt_position_address, "DebtPosition", DebtPositionLayout),
        )
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
            now_unix=view.now,
        )
        debt_raw = math_utils.debt_shares_to_assets_up(
            debt_position.borrow_shares,
            reserve.total_borrow_shares,
            projection.new_total_borrow_assets,
        )
        price = view.price(asset)
        usd_value = price.value_of(debt_raw, asset.decimals, True)
        return {
            "symbol": asset.symbol,
            "raw_debt": debt_raw,
            "ui_debt": math_utils.ui_amount(debt_raw, asset.decimals),
            "usd_value": math_utils.usd_value_ui(usd_value),
            "price": describe_price(price, view.now),
            "usd_value_nano": usd_value,
            "price_status_bits": price.status,
        }

    async def _read_token_balance(self, token_account: Pubkey) -> int:
        try:
            data = await self._client.fetch_raw_account(token_account)
        except Exception:  # noqa: BLE001 - no account yet == zero balance
            return 0
        return int.from_bytes(data[64:72], "little")


def _strip_internal(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _INTERNAL_KEYS}


def _signed_usd(nano: int) -> str:
    return ("-" if nano < 0 else "") + math_utils.usd_value_ui(abs(nano))


def _usd_to_raw(price: OraclePrice, usd_nano: int, decimals: int) -> int:
    """Raw units of an asset worth `usd_nano`, rounding down; honours a receipt's exchange rate."""
    if price.receipt_rate is None:
        return math_utils.value_to_token_amount(usd_nano, price.price.value, price.price.exponent, decimals, round_up=False)
    underlying = math_utils.value_to_token_amount(
        usd_nano, price.price.value, price.price.exponent, price.receipt_rate.liquidity_decimals, round_up=False
    )
    return price.receipt_rate.receipts_for_underlying(underlying)
