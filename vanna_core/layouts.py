"""Borsh layouts for the `vanna_credit_layer` on-chain account structs (and the oracle agent's price book).

Field lists and byte layout are transcribed 1:1 from the real IDL's `types`
section (`idl/vanna_credit_layer.json`, verified against
`vanna-credit-layer/programs/vanna_credit_layer/src/state/*.rs` at origin/main `465f21a`, 2026-10-04) — see
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

from vanna_core.constants import EMPTY_ASSET_INDEX, SCOPE_CHAIN_UNUSED  # noqa: F401 - re-exported

PUBKEY = borsh.U8[32]

ProtocolConfig = borsh.CStruct(
    "admin" / PUBKEY,
    "pending_admin" / PUBKEY,
    "treasury" / PUBKEY,
    "operating_mode" / borsh.U8,
    "max_assets_per_margin" / borsh.U8,
    "next_asset_index" / borsh.U16,
    "bump" / borsh.U8,
    # Since the 2026-10-09 upgrade token collateral is protocol-wide: every margin is scanned over these token asset
    # indexes (EMPTY_ASSET_INDEX = unused slot), in this order.
    "allowed_tokens" / borsh.U16[24],
    "max_debts_per_margin" / borsh.U8,
    "max_venues_per_margin" / borsh.U8,
    "reserved" / borsh.U8[78],
)

# Operating modes (ProtocolConfig.operating_mode) — TOOLS_CATALOGUE.md §2.5.
OPERATING_MODE_NORMAL = 0
OPERATING_MODE_BORROW_PAUSED = 1
OPERATING_MODE_WITHDRAW_ONLY = 2
OPERATING_MODE_HALTED = 3


# vanna_oracle/src/config.rs::OracleConfig — how one token is priced (Scope chain primary, Pyth fallback, optional
# second Pyth factor, klend cToken rate for receipts). 184 bytes. Since the agent split it is NOT part of AssetConfig:
# it is the `config` of a token's entry in the oracle agent's price book (below).
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

# vanna_credit_layer/src/state/asset_config.rs::AssetConfig.
# `kind` is a fieldless enum (u8): 0 = Token, 1 = Venue (Phoenix perps, held through the margin's Phoenix trader).
# `oracle` is the PROGRAM ID of the oracle agent that values this asset (not a price config).
# Two sizes live on chain: the phoenix-titan build dropped `settle_mint` (189 bytes incl. the 8-byte discriminator);
# accounts created before it keep the GMTrade-era layout (221 bytes). Both decode; `parse_asset_config` picks by size.
ASSET_KIND_TOKEN = 0
ASSET_KIND_VENUE = 1
_ASSET_CONFIG_HEAD = (
    "mint" / PUBKEY,  # a token's mint, or a venue's key (Phoenix: its exchange)
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
    "kind" / borsh.U8,
    "oracle" / PUBKEY,
)
AssetConfig = borsh.CStruct(*_ASSET_CONFIG_HEAD, "issuer_controls_accepted" / borsh.Bool, "reserved" / borsh.U8[31])
AssetConfigLegacy = borsh.CStruct(
    *_ASSET_CONFIG_HEAD,
    "settle_mint" / PUBKEY,  # GMTrade's settle token; gone from the current build
    "reserved" / borsh.U8[32],
)

ASSET_CONFIG_ACCOUNT_LEN = 189  # 8-byte discriminator + 181-byte body (current build)
ASSET_CONFIG_LEGACY_ACCOUNT_LEN = 221  # 8-byte discriminator + 213-byte body (accounts created before phoenix-titan)
ASSET_CONFIG_ACCOUNT_LENS = (ASSET_CONFIG_ACCOUNT_LEN, ASSET_CONFIG_LEGACY_ACCOUNT_LEN)


def parse_asset_config(data: bytes):
    """Decode an AssetConfig account (discriminator included) of either on-chain size."""
    layout = AssetConfigLegacy if len(data) == ASSET_CONFIG_LEGACY_ACCOUNT_LEN else AssetConfig
    return layout.parse(data[8:])

# vanna_oracle/src/tokens.rs: the oracle agent's PriceBook (zero-copy account, seed `price_book`, owned by
# `vanna_oracle`): discriminator (8) | bump u8 | count u8 | padding [2] | 32 x PriceSource. A PriceSource is
# mint (32) | decimals u8 | padding [3] | OracleConfig (184) = 220 bytes.
PRICE_BOOK_HEADER_LEN = 12
PRICE_BOOK_MAX_SOURCES = 32
PRICE_SOURCE_LEN = 220
PriceSource = borsh.CStruct(
    "mint" / PUBKEY,
    "decimals" / borsh.U8,
    "padding" / borsh.U8[3],
    "config" / OracleConfig,
)

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
    "total_supply_shares" / borsh.U64,  # 2026-10-09: initialized from the share mint's supply
    "interest_remainder_wad" / borsh.U128,
    "fee_remainder" / borsh.U64,
    "reserved" / borsh.U8[96],
)

# Caps: since the 2026-10-09 migration "no cap" is u64::MAX (it was 0, which is now a real cap of zero).
UNCAPPED = 2**64 - 1


def is_capped(cap: int) -> bool:
    return cap != UNCAPPED

# Reserve statuses (Reserve.status) — TOOLS_CATALOGUE.md §2.5.
RESERVE_STATUS_ACTIVE = 0
RESERVE_STATUS_SUPPLY_ONLY = 1
RESERVE_STATUS_REPAY_ONLY = 2
RESERVE_STATUS_FROZEN = 3


# Real margin arrays hold 24 entries each (MAX_ASSETS), not 8 — verified,
# TOOLS_CATALOGUE.md §1/§6.1.
MARGIN_ACCOUNT_MAX_ASSETS = 24

# MarginAccount ends after `bump` here on purpose (parsing ignores the trailing reserved bytes, whose length has varied
# between builds). Since the 2026-10-09 upgrade a margin keeps only its VENUES (Phoenix, vault venues) in the slots that
# held its collateral: token collateral is the protocol's `ProtocolConfig.allowed_tokens`, scanned for every margin.
# Read a margin through `vanna_core.margin_state.load_margin_state`, which joins the two the way the program does.
MarginAccount = borsh.CStruct(
    "authority" / PUBKEY,
    "status" / borsh.U8,
    "venue_count" / borsh.U8,
    "debt_count" / borsh.U8,
    "venue_asset_indexes" / borsh.U16[MARGIN_ACCOUNT_MAX_ASSETS],
    "debt_asset_indexes" / borsh.U16[MARGIN_ACCOUNT_MAX_ASSETS],
    "event_sequence" / borsh.U64,
    "bump" / borsh.U8,
)

DebtPosition = borsh.CStruct(
    "margin_account" / PUBKEY,
    "reserve" / PUBKEY,
    "borrow_shares" / borsh.U128,
    "bump" / borsh.U8,
    "reserved" / borsh.U8[48],
)

# An external program `margin_execute` may call, and the VALIDATOR agent that reviews every such call (which call
# account is the margin, which vaults are spent / received). 138 bytes incl. the discriminator.
Integration = borsh.CStruct(
    "program_id" / PUBKEY,
    "validator" / PUBKEY,
    "enabled" / borsh.Bool,
    "bump" / borsh.U8,
    "reserved" / borsh.U8[64],
)


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
# `user_reclaim_rent` (replaces the old Collateral/DebtPosition Closed/Opened events).
EVENT_RentReclaimed = borsh.CStruct(
    "margin_account" / PUBKEY,
    "mint" / PUBKEY,
    "vault_closed" / borsh.Bool,
    "debt_position_closed" / borsh.Bool,
    "timestamp" / borsh.I64,
)
# `margin_execute`: one entry per margin vault paid out of / paid into.
TokenAmount = borsh.CStruct("mint" / PUBKEY, "amount" / borsh.U64)
EVENT_MarginExecuted = borsh.CStruct(
    "margin_account" / PUBKEY,
    "program_id" / PUBKEY,
    "validator" / PUBKEY,
    "spent" / borsh.Vec(TokenAmount),
    "received" / borsh.Vec(TokenAmount),
    "borrow_health_factor_wad" / borsh.U128,
    "event_sequence" / borsh.U64,
    "timestamp" / borsh.I64,
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
# Since the 2026-10-09 upgrade a liquidation names its debt and collateral legs and its venue calls.
EVENT_Liquidated = borsh.CStruct(
    "margin_account" / PUBKEY,
    "liquidator" / PUBKEY,
    "debt_mint" / PUBKEY,
    "repaid" / borsh.U64,
    "collateral_mint" / PUBKEY,
    "seized" / borsh.U64,
    "collateral_value" / borsh.U128,
    "debt_value" / borsh.U128,
    "health_factor_wad" / borsh.U128,
    "venue_calls" / borsh.U8,
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
    "RentReclaimed": EVENT_RentReclaimed,
    "MarginExecuted": EVENT_MarginExecuted,
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
