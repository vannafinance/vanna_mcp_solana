"""Event history — decoded from Anchor events, no indexer.
Backs the `vanna_history` dispatcher (TOOLS_CATALOGUE.md §3.12).

`getSignaturesForAddress` -> one `getTransaction` per signature -> every event payload it carries: "Program data:
<base64>" log lines (`emit!`) and, since the 2026-10-09 upgrade, CPI events (`emit_cpi!`: inner instructions to the
program, as the app's history reads them), each matched by its leading 8-byte discriminator against
`vanna_core.layouts.EVENT_LAYOUTS`. No batched JSON-RPC (the fork
rejects it, per `Vanna-Solana-Copilot-Answers.pdf`) — each transaction is
fetched with its own call.
"""

from __future__ import annotations

from solders.pubkey import Pubkey

from vanna_core import math_utils
from vanna_core.assets import AssetRegistry
from vanna_core.client import SolanaClient
from vanna_core.layouts import EVENT_LAYOUTS, event_discriminator, pubkey_bytes_to_base58
from vanna_core.pdas import margin_pda

_DISCRIMINATOR_TO_NAME: dict[bytes, str] = {event_discriminator(name): name for name in EVENT_LAYOUTS}

_MARGIN_EVENT_NAMES = {
    "MarginCreated", "MarginClosed", "CollateralDeposited", "CollateralWithdrawn", "RentReclaimed",
    "Borrowed", "DebtRepaid", "MarginExecuted", "CollateralSeized", "Liquidated",
}
_EARN_EVENT_NAMES = {"LiquiditySupplied", "LiquidityRedeemed"}


class HistoryContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client

    async def _decode_events(self, address: Pubkey, limit: int) -> list[dict]:
        signatures = await self._client.get_signatures_for_address(address, limit=limit)
        events: list[dict] = []
        for entry in signatures:
            payloads = await self._client.get_transaction_event_payloads(entry.signature)
            if payloads is None:
                continue
            for raw in payloads:
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
        every = registry.all(include_unoffered=True)  # decode every past event, offered or not
        mint_to_symbol = {a.mint: a.symbol for a in every}
        reserve_to_asset = {a.reserve: a for a in every if a.has_reserve}

        raw_events = await self._decode_events(margin_address, limit)
        rows = []
        for e in raw_events:
            if e["event"] not in _MARGIN_EVENT_NAMES:
                continue
            f = e["fields"]
            row = {"event": e["event"], "signature": e["signature"], "slot": e["slot"], "timestamp": f.timestamp}

            if e["event"] in ("CollateralDeposited", "CollateralWithdrawn", "RentReclaimed"):
                mint = pubkey_bytes_to_base58(f.mint)
                symbol = mint_to_symbol.get(mint, mint)
                row["asset"] = symbol
                if hasattr(f, "amount"):
                    decimals = next((a.decimals for a in registry.all(include_unoffered=True) if a.mint == mint), 0)
                    row["amount"] = math_utils.ui_amount(f.amount, decimals)
            elif e["event"] == "MarginExecuted":
                # A call into Kamino or Jupiter: one entry per margin vault paid out of / paid into.
                from vanna_core.assets import JUPITER_V6_PROGRAM_ID, KLEND_PROGRAM_ID

                decimals_of = {a.mint: a.decimals for a in every}

                def legs(entries) -> list[dict]:
                    out = []
                    for t in entries:
                        mint = pubkey_bytes_to_base58(t.mint)
                        out.append({"asset": mint_to_symbol.get(mint, mint),
                                    "amount": math_utils.ui_amount(t.amount, decimals_of.get(mint, 0))})
                    return out

                program = pubkey_bytes_to_base58(f.program_id)
                row["program"] = {str(KLEND_PROGRAM_ID): "kamino", str(JUPITER_V6_PROGRAM_ID): "jupiter"}.get(program, program)
                row["spent"], row["received"] = legs(f.spent), legs(f.received)
                if row["spent"]:
                    row["asset"], row["amount"] = row["spent"][0]["asset"], row["spent"][0]["amount"]
            elif e["event"] in ("Borrowed", "DebtRepaid"):
                reserve = pubkey_bytes_to_base58(f.reserve)
                asset = reserve_to_asset.get(reserve)
                row["asset"] = asset.symbol if asset else reserve
                if hasattr(f, "assets") and asset:
                    row["amount"] = math_utils.ui_amount(f.assets, asset.decimals)
            elif e["event"] == "CollateralSeized":
                mint = pubkey_bytes_to_base58(f.mint)
                row["asset"] = mint_to_symbol.get(mint, mint)
                row["liquidator"] = pubkey_bytes_to_base58(f.liquidator)
                decimals = next((a.decimals for a in registry.all(include_unoffered=True) if a.mint == mint), 0)
                row["amount"] = math_utils.ui_amount(f.amount, decimals)
            elif e["event"] == "Liquidated":
                # Whole-account liquidation: every debt repaid, every collateral seized.
                row["liquidator"] = pubkey_bytes_to_base58(f.liquidator)
                decimals_of = {a.mint: a.decimals for a in every}
                debt_mint, collateral_mint = pubkey_bytes_to_base58(f.debt_mint), pubkey_bytes_to_base58(f.collateral_mint)
                row["debt_asset"] = mint_to_symbol.get(debt_mint, debt_mint)
                row["repaid"] = math_utils.ui_amount(f.repaid, decimals_of.get(debt_mint, 0))
                row["collateral_asset"] = mint_to_symbol.get(collateral_mint, collateral_mint)
                row["seized"] = math_utils.ui_amount(f.seized, decimals_of.get(collateral_mint, 0))
                row["venue_calls"] = f.venue_calls
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
