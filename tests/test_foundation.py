"""Offline foundation tests — no RPC. Layout byte-sizes, PDA derivation
against known live addresses, and math formulas against hand-computed /
live-observed examples (BUILD_PLAN.md Phase 0 "definition of done").
"""

from __future__ import annotations

from solders.pubkey import Pubkey

from vanna_core import math_utils
from vanna_core.assets import (
    ASSETS,
    EXTRA_ASSET_SYMBOLS,
    KAMINO_RECEIPTS,
    AssetRegistry,
    find_asset,
    find_receipt,
    known_mints,
)
from vanna_core.layouts import AssetConfig, MarginAccount, ProtocolConfig, Reserve
from vanna_core.pdas import asset_config_pda, protocol_config_pda, reserve_pda

PROGRAM_ID = Pubkey.from_string("H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC")


# ---------------------------------------------------------------------------
# Layout byte-sizes — verified 2026-09-28 against live accounts on the fork:
# Reserve = 372 bytes, AssetConfig = 258 bytes (both including the 8-byte
# Anchor discriminator, which these `sizeof()`s exclude).
# ---------------------------------------------------------------------------


def test_reserve_layout_size_matches_live_account():
    assert Reserve.sizeof() == 372 - 8


def test_asset_config_layout_size_matches_live_account():
    # 340-byte live account (verified on the fork) = 8-byte discriminator + 332-byte body
    # (the OracleConfig-based layout; the pre-oracle-rework account was 258 bytes).
    assert AssetConfig.sizeof() == 340 - 8


def test_margin_account_layout_has_24_slots_each():
    # authority(32) + status(1) + collateral_count(1) + debt_count(1)
    # + 2*24*2 (u16 arrays) + event_sequence(8) + bump(1) + reserved(96)
    expected = 32 + 1 + 1 + 1 + 24 * 2 * 2 + 8 + 1 + 96
    assert MarginAccount.sizeof() == expected


def test_protocol_config_layout_size():
    assert ProtocolConfig.sizeof() == 32 * 3 + 1 + 1 + 2 + 1 + 128


# ---------------------------------------------------------------------------
# PDA derivation — against known live addresses on the hosted Surfpool fork.
# ---------------------------------------------------------------------------


def test_usdc_reserve_pda_matches_live_address():
    usdc = find_asset("USDC")
    address, _ = reserve_pda(usdc.mint_pubkey, PROGRAM_ID)
    assert str(address) == "A4x4ZZn134HAodwDdFdB9hquaqhwcxqbq5mVxnTXTY4t"


def test_every_registry_asset_has_a_resolvable_asset_config_pda():
    for asset in ASSETS:
        address, bump = asset_config_pda(asset.mint_pubkey, PROGRAM_ID)
        assert 0 <= bump <= 255
        assert address != asset.mint_pubkey


def test_protocol_config_pda_is_deterministic():
    a, bump_a = protocol_config_pda(PROGRAM_ID)
    b, bump_b = protocol_config_pda(PROGRAM_ID)
    assert a == b
    assert bump_a == bump_b


# ---------------------------------------------------------------------------
# Asset registry
# ---------------------------------------------------------------------------


def test_registry_has_exactly_five_real_assets_no_token2022_stock_synthetics():
    symbols = {a.symbol for a in ASSETS}
    assert symbols == {"SOL", "USDC", "USDT", "JitoSOL", "JupSOL"}
    assert len(KAMINO_RECEIPTS) == 5


def test_known_mints_cover_bootstrap_asset_set_without_duplicates():
    mints = known_mints()
    assert len(mints) == len(set(mints))
    # layer scripts/bootstrap-fork.sh: pools SOL/USDC/USDT; collateral JitoSOL, JupSOL, JupUSD, NVDAx, TSLAx
    for symbol in ("JupUSD", "NVDAx", "TSLAx"):
        assert symbol in EXTRA_ASSET_SYMBOLS.values()
    assert {a.mint for a in ASSETS} <= set(mints)


async def test_registry_falls_back_to_known_mints_and_validates_accounts():
    from vanna_core.anchor_codec import account_discriminator
    from vanna_core.layouts import ASSET_CONFIG_ACCOUNT_LEN

    program_id = Pubkey.from_string("H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC")
    disc = account_discriminator("AssetConfig")
    valid = disc + bytes(ASSET_CONFIG_ACCOUNT_LEN - 8)
    sol_pda = str(asset_config_pda(Pubkey.from_string(ASSETS[0].mint), program_id)[0])
    usdc_pda = str(asset_config_pda(Pubkey.from_string(ASSETS[1].mint), program_id)[0])
    usdt_pda = str(asset_config_pda(Pubkey.from_string(ASSETS[2].mint), program_id)[0])

    class FakeClient:
        def __init__(self):
            self.program_id = program_id

        async def get_multiple_accounts_raw(self, addresses):
            out = {str(a): None for a in addresses}
            out[sol_pda] = (str(program_id), valid)
            out[usdc_pda] = ("11111111111111111111111111111111", valid)  # wrong owner
            out[usdt_pda] = (str(program_id), valid[:-1])  # wrong length
            return out

    registry = AssetRegistry(FakeClient())
    found = await registry._load_known_mints(ASSET_CONFIG_ACCOUNT_LEN, disc)
    assert [str(address) for address, _ in found] == [sol_pda]


def test_find_asset_accepts_wsol_alias():
    assert find_asset("wSOL").symbol == "SOL"
    assert find_asset("usdc").symbol == "USDC"
    assert find_asset("DOGE") is None


def test_find_receipt_by_symbol():
    assert find_receipt("kUSDC").underlying_symbol == "USDC"
    assert find_receipt("kNOPE") is None


# ---------------------------------------------------------------------------
# Math — interest.rs, shares.rs, health.rs, verified against live values
# observed on the fork's USDC reserve on 2026-09-28.
# ---------------------------------------------------------------------------


def test_borrow_rate_matches_live_usdc_reserve_observation():
    # Live observed USDC reserve: liquidity=2_563_110_737, borrowed=236_889_263,
    # rate_curve = (linear=1e16, jump=3e17, multiplier=3.5e18) -> borrow APR
    # observed via get_pool_stats() == "0.2961", supply APR == "0.0225"
    # (reserve_factor_bps=1000).
    util = math_utils.utilization_wad(2_563_110_737, 236_889_263)
    rate_per_second = math_utils.borrow_rate_per_second_wad(
        linear_coeff_wad=10_000_000_000_000_000,
        jump_coeff_wad=300_000_000_000_000_000,
        rate_multiplier_wad=3_500_000_000_000_000_000,
        util_wad=util,
    )
    borrow_apr = math_utils.annual_rate_pct(rate_per_second)
    assert str(borrow_apr) == "0.2961"
    supply_apr = math_utils.supply_apr_pct(borrow_apr, util, reserve_factor_bps=1000)
    assert str(supply_apr) == "0.0225"


def test_utilization_wad_zero_pool():
    assert math_utils.utilization_wad(0, 0) == 0


def test_wad_pow_identity():
    assert math_utils.wad_pow(math_utils.WAD, 32) == math_utils.WAD
    assert math_utils.wad_pow(0, 5) == 0
    assert math_utils.wad_pow(0, 0) == math_utils.WAD


def test_share_round_trip_first_deposit_is_1_to_1():
    shares = math_utils.assets_to_supply_shares_down(1_000_000, total_share_supply=0, lender_total_assets_before=0)
    assert shares == 1_000_000


def test_share_round_trip_rounds_against_the_lender():
    # 3 assets in, 10 total shares, 33 lender_total_assets -> floor(3*10/33) = 0
    shares = math_utils.assets_to_supply_shares_down(3, total_share_supply=10, lender_total_assets_before=33)
    assert shares == 0


def test_health_no_debt_is_none_not_infinity():
    snap = math_utils.calculate_health([1_000], [])
    assert snap.health_factor_wad is None
    assert snap.is_borrow_healthy is True
    assert snap.is_liquidatable is False


def test_health_exactly_at_threshold_is_liquidatable():
    # collateral / debt == 1.10 exactly -> liquidatable (equality counts)
    debt = 1_000_000_000
    collateral = math_utils.mul_div_floor(debt, math_utils.BALANCE_TO_BORROW_THRESHOLD_WAD, math_utils.WAD)
    snap = math_utils.calculate_health([collateral], [debt])
    assert snap.health_factor_wad == math_utils.BALANCE_TO_BORROW_THRESHOLD_WAD
    assert snap.is_liquidatable is True
    assert snap.is_borrow_healthy is False


def test_health_comfortably_above_threshold_is_healthy():
    snap = math_utils.calculate_health([2_000], [1_000])
    assert snap.is_borrow_healthy is True
    assert snap.is_liquidatable is False


def test_debt_shares_round_up_never_under_counts():
    # 1 share out of 3 total shares, 10 total borrow assets -> ceil(1*10/3) = 4
    debt = math_utils.debt_shares_to_assets_up(1, total_borrow_shares=3, total_borrow_assets=10)
    assert debt == 4


def test_idl_hash_matches_pin():
    import pathlib

    from vanna_core.client import compute_idl_sha256
    from vanna_core.config import VannaSettings

    idl = pathlib.Path(__file__).resolve().parent.parent / "idl" / "vanna_lending.json"
    assert compute_idl_sha256(idl) == VannaSettings().pinned_idl_sha256  # type: ignore[call-arg]
