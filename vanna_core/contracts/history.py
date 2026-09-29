"""Event history — decoded from Anchor's `emit!` log lines, no indexer.
Backs the `vanna_history` dispatcher (TOOLS_CATALOGUE.md §3.12).

`getSignaturesForAddress` -> one `getTransaction` per signature -> parse each
"Program data: <base64>" log line, matching its leading 8-byte discriminator
against `vanna_core.layouts.EVENT_LAYOUTS`. No batched JSON-RPC (the fork
rejects it, per `Vanna-Solana-Copilot-Answers.pdf`) — each transaction is
fetched with its own call.
"""

from __future__ import annotations

import base64

from solders.pubkey import Pubkey

from vanna_core import math_utils
from vanna_core.assets import AssetRegistry
from vanna_core.client import SolanaClient
from vanna_core.layouts import EVENT_LAYOUTS, event_discriminator, pubkey_bytes_to_base58
from vanna_core.pdas import margin_pda

_DISCRIMINATOR_TO_NAME: dict[bytes, str] = {event_discriminator(name): name for name in EVENT_LAYOUTS}

_MARGIN_EVENT_NAMES = {
    "MarginCreated", "MarginClosed", "CollateralDeposited", "CollateralWithdrawn",
    "CollateralPositionClosed", "DebtPositionOpened", "DebtPositionClosed",
    "Borrowed", "DebtRepaid", "CollateralSeized", "Liquidated",
}
_EARN_EVENT_NAMES = {"LiquiditySupplied", "LiquidityRedeemed"}


class HistoryContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client

    async def _decode_events(self, address: Pubkey, limit: int) -> list[dict]:
        signatures = await self._client.get_signatures_for_address(address, limit=limit)
        events: list[dict] = []
        for entry in signatures:
            logs = await self._client.get_transaction_logs(entry.signature)
            if logs is None:
                continue
            for log in logs:
                if not log.startswith("Program data: "):
                    continue
                try:
                    raw = base64.b64decode(log.removeprefix("Program data: "))
                except Exception:  # noqa: BLE001
                    continue
                if len(raw) < 8:
                    continue
                name = _DISCRIMINATOR_TO_NAME.get(raw[:8])
                if name is None:
                    continue
                try:
                    decoded = EVENT_LAYOUTS[name].parse(raw[8:])
                except Exception:  # noqa: BLE001
                    continue
                events.append(
                    {
                        "event": name,
                        "signature": str(entry.signature),
                        "slot": entry.slot,
                        "err": entry.err is not None,
                        "fields": decoded,
                    }
                )
        return events

    async def get_margin_history(self, authority: Pubkey, limit: int = 50) -> dict:
        margin_address = margin_pda(authority, self._client.program_id)[0]
        registry = AssetRegistry(self._client)
        await registry.load()
        mint_to_symbol = {a.mint: a.symbol for a in registry.all()}
        reserve_to_asset = {a.reserve: a for a in registry.all() if a.has_reserve}

        raw_events = await self._decode_events(margin_address, limit)
        rows = []
        for e in raw_events:
            if e["event"] not in _MARGIN_EVENT_NAMES:
                continue
            f = e["fields"]
            row = {"event": e["event"], "signature": e["signature"], "slot": e["slot"], "timestamp": f.timestamp}

            if e["event"] in ("CollateralDeposited", "CollateralWithdrawn", "CollateralPositionClosed"):
                mint = pubkey_bytes_to_base58(f.mint)
                symbol = mint_to_symbol.get(mint, mint)
                row["asset"] = symbol
                if hasattr(f, "amount"):
                    decimals = next((a.decimals for a in registry.all() if a.mint == mint), 0)
                    row["amount"] = math_utils.ui_amount(f.amount, decimals)
            elif e["event"] in ("Borrowed", "DebtRepaid", "DebtPositionOpened", "DebtPositionClosed"):
                reserve = pubkey_bytes_to_base58(f.reserve)
                asset = reserve_to_asset.get(reserve)
                row["asset"] = asset.symbol if asset else reserve
                if hasattr(f, "assets") and asset:
                    row["amount"] = math_utils.ui_amount(f.assets, asset.decimals)
            elif e["event"] == "CollateralSeized":
                mint = pubkey_bytes_to_base58(f.mint)
                row["asset"] = mint_to_symbol.get(mint, mint)
                row["liquidator"] = pubkey_bytes_to_base58(f.liquidator)
                decimals = next((a.decimals for a in registry.all() if a.mint == mint), 0)
                row["amount"] = math_utils.ui_amount(f.amount, decimals)
            elif e["event"] == "Liquidated":
                # Whole-account liquidation: every debt repaid, every collateral seized.
                row["liquidator"] = pubkey_bytes_to_base58(f.liquidator)
                row["collaterals_seized"] = f.collaterals_seized
                row["debts_repaid"] = f.debts_repaid
                row["collateral_value_usd"] = math_utils.usd_value_ui(f.collateral_value)
                row["debt_value_usd"] = math_utils.usd_value_ui(f.debt_value)
                row["health_factor"] = str(f.health_factor_wad / math_utils.WAD)

            if hasattr(f, "borrow_health_factor_wad"):
                row["post_action_health_factor"] = str(f.borrow_health_factor_wad / math_utils.WAD)

            rows.append(row)
        return {"margin_account": str(margin_address), "events": rows, "complete": len(raw_events) < limit}

    async def get_earn_history(self, owner: Pubkey, asset_symbol: str, limit: int = 50) -> dict:
        registry = AssetRegistry(self._client)
        await registry.load()
        asset = registry.by_symbol(asset_symbol)
        reserve_address = Pubkey.from_string(asset.reserve)

        raw_events = await self._decode_events(reserve_address, limit)
        owner_b58 = str(owner)
        rows = []
        for e in raw_events:
            if e["event"] not in _EARN_EVENT_NAMES:
                continue
            f = e["fields"]
            if pubkey_bytes_to_base58(f.lender) != owner_b58:
                continue
            rows.append(
                {
                    "event": e["event"],
                    "signature": e["signature"],
                    "slot": e["slot"],
                    "timestamp": f.timestamp,
                    "assets": math_utils.ui_amount(f.assets, asset.decimals),
                    "shares": math_utils.ui_amount(f.shares, asset.decimals),
                }
            )
        return {
            "reserve": str(reserve_address),
            "asset": asset.symbol,
            "events": rows,
            "complete": len(raw_events) < limit,
        }
