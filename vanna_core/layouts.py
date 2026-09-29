"""Borsh layouts for vanna_lending on-chain account structs.

Field lists and byte layout are transcribed 1:1 from the real IDL's `types`
section (`idl/vanna_lending.json`, verified against
`vanna-credit-layer/programs/vanna_lending/src/state/*.rs` in the
`Vanna-Solana-Copilot-Answers.pdf` handoff, 2026-09-28) — see
TOOLS_CATALOGUE.md §1 for the PDA each struct belongs to. Anchor emits
account fields in declared order with no alignment padding (Borsh has none).

Every struct's tail `reserved` bytes exist on-chain for future fields; they
are parsed (to consume the correct byte length) but never interpreted.

`PriceSource` and `AdapterKind` are fieldless (C-like) Rust enums, so Borsh
serializes each as a single `u8` variant index with no payload — decoded here
as plain `U8`, not `borsh_construct.Enum`, to sidestep any tagged-union
padding assumptions. Variant order (0-indexed) matches the IDL exactly:
`PriceSource`: 0 = Pyth, 1 = KaminoReceipt. `AdapterKind`: 0 = KaminoLend,
1 = Jupiter.
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

# Operating modes (ProtocolConfig.operating_mode) — TOOLS_CATALOGUE.md §2.5.
OPERATING_MODE_NORMAL = 0
OPERATING_MODE_BORROW_PAUSED = 1
OPERATING_MODE_WITHDRAW_ONLY = 2
OPERATING_MODE_HALTED = 3

SCOPE_CHAIN_UNUSED = 0xFFFF

# state/asset_config.rs::OracleConfig — how one asset is priced (Scope chain primary,
# Pyth fallback, optional second Pyth factor, klend cToken rate for receipts).
OracleConfig = borsh.CStruct(
    "scope_prices" / PUBKEY,
    "scope_chain" / borsh.U16[4],
    "scope_twap_chain" / borsh.U16[4],
    "pyth_price" / PUBKEY,
    "pyth_factor" / PUBKEY,
    "klend_reserve" / PUBKEY,
    "klend_program" / PUBKEY,
    "max_age_secs" / borsh.U32,
    "max_twap_divergence_bps" / borsh.U16,
    "max_confidence_bps" / borsh.U16,
)

# state/asset_config.rs::AssetConfig (340 bytes incl. the 8-byte discriminator).
AssetConfig = borsh.CStruct(
    "mint" / PUBKEY,
    "token_program" / PUBKEY,
    "reserve" / PUBKEY,
    "max_collateral_per_margin" / borsh.U64,
    "ltv_bps" / borsh.U16,  # stored, not used by V1 health math
    "liquidation_threshold_bps" / borsh.U16,  # stored, not used by V1 health math
    "liquidation_bonus_bps" / borsh.U16,  # stored; liquidation now hands over the whole account
    "asset_index" / borsh.U16,
    "decimals" / borsh.U8,
    "collateral_enabled" / borsh.Bool,
    "borrow_enabled" / borsh.Bool,
    "bump" / borsh.U8,
    "oracle" / OracleConfig,
    "reserved" / borsh.U8[32],
)

ASSET_CONFIG_ACCOUNT_LEN = 340  # 8-byte discriminator + 332-byte body (state/asset_config.rs)

RateCurve = borsh.CStruct(
    "linear_coeff_wad" / borsh.U64,
    "jump_coeff_wad" / borsh.U64,
    "rate_multiplier_wad" / borsh.U64,
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
    "rate_curve" / RateCurve,
    "reserve_factor_bps" / borsh.U16,
    "status" / borsh.U8,
    "bump" / borsh.U8,
    "reserved" / borsh.U8[128],
)

# Reserve statuses (Reserve.status) — TOOLS_CATALOGUE.md §2.5.
RESERVE_STATUS_ACTIVE = 0
RESERVE_STATUS_SUPPLY_ONLY = 1
RESERVE_STATUS_REPAY_ONLY = 2
RESERVE_STATUS_FROZEN = 3

# EMPTY_INDEX sentinel used for unused collateral/debt asset-index slots
# (mirrors lib/margin-utils.ts EMPTY_INDEX). Re-exported from assets.py too;
# defined here as well since layouts.py has no dependency on assets.py.
EMPTY_ASSET_INDEX = 65_535

# Real margin arrays hold 24 entries each (MAX_ASSETS), not 8 — verified,
# TOOLS_CATALOGUE.md §1/§6.1.
MARGIN_ACCOUNT_MAX_ASSETS = 24

MarginAccount = borsh.CStruct(
    "authority" / PUBKEY,
    "status" / borsh.U8,
    "collateral_count" / borsh.U8,
    "debt_count" / borsh.U8,
    "collateral_asset_indexes" / borsh.U16[MARGIN_ACCOUNT_MAX_ASSETS],
    "debt_asset_indexes" / borsh.U16[MARGIN_ACCOUNT_MAX_ASSETS],
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

Integration = borsh.CStruct(
    "program_id" / PUBKEY,
    "adapter" / borsh.U8,  # 0 = KaminoLend, 1 = Jupiter
    "enabled" / borsh.Bool,
    "bump" / borsh.U8,
    "reserved" / borsh.U8[64],
)

ADAPTER_KIND_KAMINO_LEND = 0
ADAPTER_KIND_JUPITER = 1


# ---------------------------------------------------------------------------
# Pyth Receiver's PriceUpdateV2 account — owned by the Pyth Receiver program
# (`rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ`), not the Vanna program.
# Verified against the real IDL's `types` section (Pyth's own account type,
# re-declared there because the program CPIs into the receiver).
# ---------------------------------------------------------------------------

PriceFeedMessage = borsh.CStruct(
    "feed_id" / borsh.U8[32],
    "price" / borsh.I64,
    "conf" / borsh.U64,
    "exponent" / borsh.I32,
    "publish_time" / borsh.I64,
    "prev_publish_time" / borsh.I64,
    "ema_price" / borsh.I64,
    "ema_conf" / borsh.U64,
)

# Tagged union: 0 = Partial{num_signatures: u8}, 1 = Full (no payload).
VerificationLevel = borsh.Enum(
    "Partial" / borsh.CStruct("num_signatures" / borsh.U8),
    "Full" / borsh.CStruct(),
    enum_name="VerificationLevel",
)

PriceUpdateV2 = borsh.CStruct(
    "write_authority" / PUBKEY,
    "verification_level" / VerificationLevel,
    "price_message" / PriceFeedMessage,
    "posted_slot" / borsh.U64,
)


# ---------------------------------------------------------------------------
# Events — decoded from "Program data: <base64>" log lines (Anchor's default
# `emit!` mechanism, no `event_cpi`). Layout = 8-byte discriminator
# (`sha256("event:<Name>")[:8]`, same formula as account_discriminator with a
# different prefix) + borsh-serialized fields, verified against the real
# IDL's `events` section. Used by `vanna_history` (TOOLS_CATALOGUE.md §3.12).
# ---------------------------------------------------------------------------

EVENT_MarginCreated = borsh.CStruct(
    "margin_account" / PUBKEY, "authority" / PUBKEY, "timestamp" / borsh.I64
)
EVENT_MarginClosed = borsh.CStruct(
    "margin_account" / PUBKEY, "authority" / PUBKEY, "timestamp" / borsh.I64
)
EVENT_CollateralDeposited = borsh.CStruct(
    "margin_account" / PUBKEY,
    "mint" / PUBKEY,
    "amount" / borsh.U64,
    "new_collateral_amount" / borsh.U64,
    "event_sequence" / borsh.U64,
    "timestamp" / borsh.I64,
)
EVENT_CollateralWithdrawn = borsh.CStruct(
    "margin_account" / PUBKEY,
    "mint" / PUBKEY,
    "amount" / borsh.U64,
    "new_collateral_amount" / borsh.U64,
    "borrow_health_factor_wad" / borsh.U128,
    "event_sequence" / borsh.U64,
    "timestamp" / borsh.I64,
)
EVENT_CollateralPositionClosed = borsh.CStruct(
    "margin_account" / PUBKEY, "mint" / PUBKEY, "timestamp" / borsh.I64
)
EVENT_DebtPositionOpened = borsh.CStruct(
    "margin_account" / PUBKEY, "reserve" / PUBKEY, "timestamp" / borsh.I64
)
EVENT_DebtPositionClosed = borsh.CStruct(
    "margin_account" / PUBKEY, "reserve" / PUBKEY, "timestamp" / borsh.I64
)
EVENT_Borrowed = borsh.CStruct(
    "margin_account" / PUBKEY,
    "reserve" / PUBKEY,
    "assets" / borsh.U64,
    "debt_shares" / borsh.U128,
    "total_debt_shares" / borsh.U128,
    "borrow_health_factor_wad" / borsh.U128,
    "event_sequence" / borsh.U64,
    "timestamp" / borsh.I64,
)
EVENT_DebtRepaid = borsh.CStruct(
    "margin_account" / PUBKEY,
    "reserve" / PUBKEY,
    "payer" / PUBKEY,
    "assets" / borsh.U64,
    "debt_shares_burned" / borsh.U128,
    "remaining_debt_shares" / borsh.U128,
    "event_sequence" / borsh.U64,
    "timestamp" / borsh.I64,
)
# Whole-account liquidation (events.rs): one CollateralSeized per collateral swept, DebtRepaid per
# debt repaid, then Liquidated with the pre-liquidation snapshot values (nano-USD).
EVENT_CollateralSeized = borsh.CStruct(
    "margin_account" / PUBKEY,
    "liquidator" / PUBKEY,
    "mint" / PUBKEY,
    "amount" / borsh.U64,
    "destination" / PUBKEY,
    "event_sequence" / borsh.U64,
    "timestamp" / borsh.I64,
)
EVENT_Liquidated = borsh.CStruct(
    "margin_account" / PUBKEY,
    "liquidator" / PUBKEY,
    "collateral_value" / borsh.U128,
    "debt_value" / borsh.U128,
    "health_factor_wad" / borsh.U128,
    "collaterals_seized" / borsh.U8,
    "debts_repaid" / borsh.U8,
    "event_sequence" / borsh.U64,
    "timestamp" / borsh.I64,
)
EVENT_LiquiditySupplied = borsh.CStruct(
    "reserve" / PUBKEY,
    "lender" / PUBKEY,
    "assets" / borsh.U64,
    "shares" / borsh.U64,
    "total_borrow_assets" / borsh.U64,
    "accounted_liquidity_assets" / borsh.U64,
    "timestamp" / borsh.I64,
)
EVENT_LiquidityRedeemed = borsh.CStruct(
    "reserve" / PUBKEY,
    "lender" / PUBKEY,
    "assets" / borsh.U64,
    "shares" / borsh.U64,
    "total_borrow_assets" / borsh.U64,
    "accounted_liquidity_assets" / borsh.U64,
    "timestamp" / borsh.I64,
)

# name -> layout, for every event `vanna_history` decodes.
EVENT_LAYOUTS: dict[str, "borsh.CStruct"] = {
    "MarginCreated": EVENT_MarginCreated,
    "MarginClosed": EVENT_MarginClosed,
    "CollateralDeposited": EVENT_CollateralDeposited,
    "CollateralWithdrawn": EVENT_CollateralWithdrawn,
    "CollateralPositionClosed": EVENT_CollateralPositionClosed,
    "DebtPositionOpened": EVENT_DebtPositionOpened,
    "DebtPositionClosed": EVENT_DebtPositionClosed,
    "Borrowed": EVENT_Borrowed,
    "DebtRepaid": EVENT_DebtRepaid,
    "CollateralSeized": EVENT_CollateralSeized,
    "Liquidated": EVENT_Liquidated,
    "LiquiditySupplied": EVENT_LiquiditySupplied,
    "LiquidityRedeemed": EVENT_LiquidityRedeemed,
}


def event_discriminator(event_name: str) -> bytes:
    import hashlib

    return hashlib.sha256(f"event:{event_name}".encode()).digest()[:8]


def pubkey_bytes_to_base58(raw: bytes) -> str:
    from solders.pubkey import Pubkey

    return str(Pubkey(bytes(raw)))
