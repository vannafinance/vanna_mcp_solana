"""Offline foundation tests — no RPC. Layout byte-sizes, PDA derivation
against known live addresses, and math formulas against hand-computed /
live-observed examples.
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
    # The phoenix-titan build dropped `settle_mint`: 189 bytes = 8-byte discriminator + 181-byte body. Accounts created
    # before it keep the 221-byte layout (verified on the fork, 2026-10-07); both decode by size.
    from vanna_core.layouts import AssetConfigLegacy, parse_asset_config

    assert AssetConfig.sizeof() == 189 - 8
    assert AssetConfigLegacy.sizeof() == 221 - 8
    head = bytes(8) + bytes(32 * 3) + (7).to_bytes(8, "little")
    for size in (189, 221):
        data = head + bytes(size - len(head))
        assert parse_asset_config(data).max_collateral_per_margin == 7


def test_margin_account_layout_has_24_slots_each():
    # authority(32) + status(1) + collateral_count(1) + debt_count(1) + 2*24*2 (u16 arrays) + event_sequence(8) + bump(1).
    # The layout stops at `bump`: what follows differs between program builds (see layouts.MarginAccount).
    assert MarginAccount.sizeof() == 32 + 1 + 1 + 1 + 24 * 2 * 2 + 8 + 1


def test_margin_account_decodes_venues_whatever_the_trailing_bytes():
    # Since 2026-10-09 the slots that held collateral hold only venues; trailing reserved bytes have varied by build.
    venues = (17).to_bytes(2, "little") + (65535).to_bytes(2, "little") * 23
    debt = (0).to_bytes(2, "little") + (65535).to_bytes(2, "little") * 23
    head = bytes(32) + bytes([0, 1, 1]) + venues + debt + (7).to_bytes(8, "little") + bytes([254])
    for tail in (bytes(36), bytes(6 * 10 + 36)):
        m = MarginAccount.parse(head + tail)
        assert (m.venue_count, m.debt_count, list(m.venue_asset_indexes[:2]), m.event_sequence, m.bump) == (1, 1, [17, 65535], 7, 254)


def test_protocol_config_layout_size():
    # Same 237-byte account as before the upgrade: allowed_tokens and the two limits came out of `reserved`.
    assert ProtocolConfig.sizeof() == 32 * 3 + 1 + 1 + 2 + 1 + 24 * 2 + 1 + 1 + 78 == 229


def test_margin_state_joins_allowed_tokens_and_venues():
    from solders.pubkey import Pubkey

    from vanna_core.margin_state import MarginState

    state = MarginState(
        address=Pubkey.new_unique(), authority=None, status=0, venue_count=1, debt_count=1,
        venue_asset_indexes=[17] + [65535] * 23, debt_asset_indexes=[0] + [65535] * 23, event_sequence=1,
        allowed_token_indexes=[0, 1, 2], max_debts_per_margin=3, max_venues_per_margin=2, vault={0: 5, 2: 0},
    )
    # Health groups walk every allowed token (zero balances too), then the venue slots.
    assert state.collateral_asset_indexes[:4] == [0, 1, 2, 17]
    # Only what is held is valued: USDC (0) with a balance and the venue.
    assert state.held_collateral_indexes == [0, 17]
    assert state.collateral_count == 2
    assert state.with_vault(1, 10).held_collateral_indexes == [0, 1, 17]
    assert state.is_allowed(2) and not state.is_allowed(5)


def test_caps_use_u64_max_for_none():
    from vanna_core.layouts import UNCAPPED, is_capped

    assert not is_capped(UNCAPPED)
    assert is_capped(0) and is_capped(1_000)


def test_error_markers_come_from_the_idl():
    from vanna_core.program_errors import code, markers

    assert code("StalePrice") == 6024
    assert "custom(6024)" in markers("StalePrice") and "0x1788" in markers("StalePrice")


def test_wallet_table_keys_skip_signers_tables_and_the_margin():
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    from vanna_core.wallet_alt import wanted_keys

    payer, margin, vault, shared, program = (Pubkey.new_unique() for _ in range(5))
    ix = Instruction(program, b"", [AccountMeta(payer, True, True), AccountMeta(margin, False, True),
                                    AccountMeta(vault, False, True), AccountMeta(shared, False, False)])
    table = AddressLookupTableAccount(key=Pubkey.new_unique(), addresses=[shared, program])
    assert wanted_keys([ix], payer, [table], {margin}) == [vault]


async def test_wallet_table_memo_counts_only_when_the_wallet_paid():
    from types import SimpleNamespace

    from solders.pubkey import Pubkey

    from vanna_core.wallet_alt import MEMO_PREFIX, _from_history

    wallet, attacker = Pubkey.new_unique(), Pubkey.new_unique()
    planted, own = Pubkey.new_unique(), Pubkey.new_unique()
    history = [SimpleNamespace(signature="s1", err=None, memo=f"[12] {MEMO_PREFIX}{planted}"),
               SimpleNamespace(signature="s2", err=None, memo=f"[12] {MEMO_PREFIX}{own}")]
    payers = {"s1": str(attacker), "s2": str(wallet)}

    class Client:
        async def get_signatures_for_address(self, address, limit):
            return history

        async def get_transaction_fee_payer(self, signature):
            return payers[signature]

    assert await _from_history(Client(), wallet) == str(own)  # the newer, attacker-paid memo is skipped
    payers["s2"] = str(attacker)
    assert await _from_history(Client(), wallet) is None


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

    idl = pathlib.Path(__file__).resolve().parent.parent / "idl" / "vanna_credit_layer.json"
    assert compute_idl_sha256(idl) == VannaSettings().pinned_idl_sha256  # type: ignore[call-arg]


async def test_registry_caches_discovery_and_does_not_retry_get_program_accounts():
    from types import SimpleNamespace

    import vanna_core.assets as assets_mod
    from vanna_core.anchor_codec import account_discriminator
    from vanna_core.layouts import ASSET_CONFIG_ACCOUNT_LEN

    assets_mod.clear_registry_cache()
    program_id = Pubkey.from_string("H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC")
    valid = account_discriminator("AssetConfig") + bytes(ASSET_CONFIG_ACCOUNT_LEN - 8)
    sol_pda = str(asset_config_pda(Pubkey.from_string(ASSETS[0].mint), program_id)[0])

    class FakeClient:
        def __init__(self):
            self.program_id = program_id
            self.settings = SimpleNamespace(
                solana_rpc_url="http://fake-rpc-for-cache-test",
                vanna_oracle_program_id="FXY5DRfekMUTbUp4uyCFpCZmCccrp6kPq3hTM4GRihnc",
            )
            self.gpa_calls = 0
            self.batch_calls = 0

        async def get_program_accounts(self, *_a, **_k):
            self.gpa_calls += 1
            raise RuntimeError("Internal error")

        async def get_multiple_accounts_raw(self, addresses):
            self.batch_calls += 1
            out = {str(a): None for a in addresses}
            out[sol_pda] = (str(program_id), valid)
            return out

    client = FakeClient()
    first = AssetRegistry(client)
    await first.load()
    assert first.discovery_mode == "known_mints"
    second = AssetRegistry(client)
    await second.load()  # within the TTL: no RPC at all
    assert (client.gpa_calls, client.batch_calls) == (1, 2)  # known mints + the oracle price book

    assets_mod._registry_cache.clear()  # TTL expiry: re-read, but getProgramAccounts stays skipped
    third = AssetRegistry(client)
    await third.load()
    assert (client.gpa_calls, client.batch_calls) == (1, 4)
    assets_mod.clear_registry_cache()


def test_product_scope_is_the_apps_tokens_and_their_receipts():
    from types import SimpleNamespace

    from vanna_core.assets import (
        KAMINO_RECEIPTS,
        NAMED_NOT_OFFERED,
        PRODUCT_SYMBOLS,
        VENUE_SYMBOLS,
        collateral_only_symbols,
        is_offered,
        symbol_decimals,
    )

    assert PRODUCT_SYMBOLS == {"SOL", "USDC", "USDT", "JitoSOL", "JupSOL", "JupUSD", "NVDAx", "TSLAx", "GOOGLx", "AAPLx"}
    for asset in ASSETS:
        assert is_offered(SimpleNamespace(symbol=asset.symbol, mint=asset.mint, is_venue=False))
    for receipt in KAMINO_RECEIPTS:
        assert is_offered(SimpleNamespace(symbol=receipt.symbol, mint=receipt.collateral_mint, is_venue=False))
    for mint, symbol in EXTRA_ASSET_SYMBOLS.items():
        offered = is_offered(SimpleNamespace(symbol=symbol, mint=mint, is_venue=symbol in VENUE_SYMBOLS))
        # Venues (Phoenix, GMTrade) and phUSD (Phoenix collateral in transit) are named for reads, never offered.
        assert offered is (symbol not in VENUE_SYMBOLS | NAMED_NOT_OFFERED), symbol
    # A registered asset nobody named is not offered.
    assert not is_offered(SimpleNamespace(symbol="asset#42", mint=Pubkey.new_unique(), is_venue=False))
    assert set(collateral_only_symbols()) == {"JitoSOL", "JupSOL", "JupUSD", "NVDAx", "TSLAx", "GOOGLx", "AAPLx"}
    assert [symbol_decimals(s) for s in ("SOL", "USDC", "JupUSD", "NVDAx", "AAPLx", "kSOL")] == [9, 6, 6, 8, 8, 6]


def test_deposit_and_farm_rules_follow_the_app():
    import pytest

    from vanna_core.assets import assert_depositable, assert_farm_listed
    from vanna_core.types import AssetNotOfferedError

    for symbol in ("SOL", "USDC", "JitoSOL", "JupUSD", "NVDAx"):
        assert_depositable(symbol)
    with pytest.raises(AssetNotOfferedError, match="not offered as a margin collateral deposit"):
        assert_depositable("usdt")
    for symbol in ("SOL", "USDC", "USDT"):
        assert_farm_listed(symbol)
    for symbol in ("JitoSOL", "JupSOL"):
        with pytest.raises(AssetNotOfferedError, match="closed to new supply"):
            assert_farm_listed(symbol)


async def test_an_unsigned_wallet_table_setup_is_rebuilt_not_blocked(monkeypatch):
    """The Copilot's dry build returns the one-time setup; nobody signs it; building again must return the same setup
    (same table address) instead of 'hasn't landed yet'."""
    from solders.pubkey import Pubkey

    from vanna_core import wallet_alt

    wallet = Pubkey.new_unique()
    keys = [Pubkey.new_unique() for _ in range(3)]

    class Client:
        slot = 1_000

        async def get_slot(self):
            return self.slot

        async def fetch_raw_account_or_none(self, key):
            return None  # the setup never landed

        async def get_signatures_for_address(self, address, limit):
            return []

        async def build_write_envelope(self, instructions, payer, purpose, summary):
            return {"instructions": instructions, "summary": summary}

    async def stable(_client, _wallet, _registry):
        return keys

    monkeypatch.setattr(wallet_alt, "stable_keys", stable)
    wallet_alt.clear_wallet_table_cache()
    client = Client()
    first = await wallet_alt.setup_envelope_if_needed(client, wallet, registry=object())
    client.slot += 40
    second = await wallet_alt.setup_envelope_if_needed(client, wallet, registry=object())
    assert first["summary"]["lookup_table"] == second["summary"]["lookup_table"]
    client.slot += 400  # the creation slot is too old to reuse: a fresh table address
    third = await wallet_alt.setup_envelope_if_needed(client, wallet, registry=object())
    assert third["summary"]["lookup_table"] != first["summary"]["lookup_table"]
    wallet_alt.clear_wallet_table_cache()
