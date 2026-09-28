"""Borsh layouts for vanna_lending on-chain account structs.

Field lists and byte layout are transcribed 1:1 from idl/vanna_lending.json
`accounts`/`types` (Anchor emits accounts in declared-field order, no padding
beyond each primitive's natural size — Borsh has no alignment padding). See
VANNA_SOLANA_MCP_REQUIREMENTS.md (Backend-Solana repo) Part B for the field
list next to each PDA it belongs to.

Every struct's tail `reserved` bytes exist on-chain for future fields; they are
parsed (to consume the correct byte length) but never interpreted.
"""

from __future__ import annotations

import borsh_construct as borsh

PUBKEY = borsh.U8[32]

ProtocolConfig = borsh.CStruct(
    "admin" / PUBKEY,
    "pending_admin" / PUBKEY,
    "treasury" / PUBKEY,
    "operating_mode" / borsh.U8,
    "max_assets_per_margin" / borsh.U8,
    "next_asset_index" / borsh.U16,
    "bump" / borsh.U8,
    "reserved" / borsh.U8[128],
)

AssetConfig = borsh.CStruct(
    "mint" / PUBKEY,
    "token_program" / PUBKEY,
    "reserve" / PUBKEY,
    "price_feed_id" / borsh.U8[32],
    "max_collateral_per_margin" / borsh.U64,
    "ltv_bps" / borsh.U16,
    "liquidation_threshold_bps" / borsh.U16,
    "liquidation_bonus_bps" / borsh.U16,
    "max_confidence_bps" / borsh.U16,
    "max_price_age_secs" / borsh.U32,
    "asset_index" / borsh.U16,
    "decimals" / borsh.U8,
    "collateral_enabled" / borsh.Bool,
    "borrow_enabled" / borsh.Bool,
    "bump" / borsh.U8,
    "reserved" / borsh.U8[96],
)

Reserve = borsh.CStruct(
    "asset_config" / PUBKEY,
    "underlying_mint" / PUBKEY,
    "liquidity_vault" / PUBKEY,
    "share_mint" / PUBKEY,
    "supply_cap" / borsh.U64,
    "borrow_cap" / borsh.U64,
    "accounted_liquidity_assets" / borsh.U64,
    "total_borrow_assets" / borsh.U64,
    "total_borrow_shares" / borsh.U128,
    "borrow_index_wad" / borsh.U128,
    "accrued_protocol_fees" / borsh.U64,
    "last_update_timestamp" / borsh.I64,
    "base_rate_bps" / borsh.U16,
    "slope1_bps" / borsh.U16,
    "slope2_bps" / borsh.U16,
    "optimal_utilization_bps" / borsh.U16,
    "reserve_factor_bps" / borsh.U16,
    "status" / borsh.U8,
    "bump" / borsh.U8,
    "reserved" / borsh.U8[128],
)

# EMPTY_INDEX sentinel used for unused collateral/debt asset-index slots
# (mirrors lib/margin-utils.ts EMPTY_INDEX).
EMPTY_ASSET_INDEX = 65_535

MarginAccount = borsh.CStruct(
    "authority" / PUBKEY,
    "status" / borsh.U8,
    "collateral_count" / borsh.U8,
    "debt_count" / borsh.U8,
    "collateral_asset_indexes" / borsh.U16[8],
    "debt_asset_indexes" / borsh.U16[8],
    "event_sequence" / borsh.U64,
    "bump" / borsh.U8,
    "reserved" / borsh.U8[96],
)

DebtPosition = borsh.CStruct(
    "margin_account" / PUBKEY,
    "reserve" / PUBKEY,
    "borrow_shares" / borsh.U128,
    "bump" / borsh.U8,
    "reserved" / borsh.U8[48],
)


def pubkey_bytes_to_base58(raw: bytes) -> str:
    from solders.pubkey import Pubkey

    return str(Pubkey(bytes(raw)))
