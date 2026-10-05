"""Offline tests for the health-account builder (`vanna_core.remaining_accounts`) and the
`dev-fork` refresh helpers. The layouts asserted here come straight from the program
source (`risk_engine.rs::{split_positions, scan_positions}`): position groups first, in the
margin's slot order, then the oracle accounts - no price accounts inside the groups."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey

from vanna_core import pricing as pr
from vanna_core.assets import DiscoveredAsset
from vanna_core.contracts import oracle_refresh as refresh
from vanna_core.pdas import debt_position_pda, find_associated_token_address, price_book_pda
from vanna_core.pricing import OracleSpec
from vanna_core.remaining_accounts import build_health_accounts, oracle_account_metas, position_groups

EMPTY = 65_535
U = pr.SCOPE_CHAIN_UNUSED
PROGRAM = Pubkey.new_unique()
MARGIN = Pubkey.new_unique()
TOKEN_PROGRAM = str(Pubkey.new_unique())
SCOPE = str(Pubkey.new_unique())


def make_asset(symbol: str, index: int, *, scope=True, pyth=True, factor=False, klend=False, has_reserve=True) -> DiscoveredAsset:
    pyth_key, factor_key, klend_key = (str(Pubkey.new_unique()) if flag else "" for flag in (pyth, factor, klend))
    return DiscoveredAsset(
        symbol=symbol, asset_index=index, config_address=str(Pubkey.new_unique()), mint=str(Pubkey.new_unique()),
        reserve=str(Pubkey.new_unique()) if has_reserve else "11111111111111111111111111111111",
        decimals=6, token_program=TOKEN_PROGRAM, collateral_enabled=True, borrow_enabled=has_reserve,
        oracle=OracleSpec(
            scope_prices=SCOPE if scope else "", scope_chain=(index, U, U, U) if scope else (U, U, U, U),
            scope_twap_chain=(index + 1, U, U, U) if scope else (U, U, U, U),
            pyth_price=pyth_key, pyth_factor=factor_key, klend_reserve=klend_key,
            klend_program=str(Pubkey.new_unique()) if klend else "", max_age_secs=120,
            max_twap_divergence_bps=1000, max_confidence_bps=200,
        ),
        max_collateral_per_margin=0, liquidation_bonus_bps=0,
    )


class FakeRegistry:
    def __init__(self, *assets: DiscoveredAsset) -> None:
        self._by_index = {a.asset_index: a for a in assets}

    async def by_index(self, index: int) -> DiscoveredAsset:
        return self._by_index[index]


def margin_with(collateral: list[int], debt: list[int]):
    pad = lambda xs: [*xs, *([EMPTY] * (24 - len(xs)))]  # noqa: E731
    return SimpleNamespace(collateral_asset_indexes=pad(collateral), debt_asset_indexes=pad(debt))


ORACLE_PROGRAM = "FXY5DRfekMUTbUp4uyCFpCZmCccrp6kPq3hTM4GRihnc"
CLIENT = SimpleNamespace(program_id=PROGRAM, settings=SimpleNamespace(vanna_oracle_program_id=ORACLE_PROGRAM))


def keys(metas):
    return [str(m.pubkey) for m in metas]


async def test_groups_have_no_price_accounts_and_follow_slot_order():
    sol, usdc, jito = make_asset("SOL", 3), make_asset("USDC", 13), make_asset("JitoSOL", 210)
    registry = FakeRegistry(sol, usdc, jito)
    margin = margin_with(collateral=[13, 210], debt=[3])  # note: slot order, not sorted by index

    groups = await position_groups(CLIENT, MARGIN, margin, registry, set(), None)

    def vault(asset):
        return find_associated_token_address(MARGIN, Pubkey.from_string(asset.mint), Pubkey.from_string(TOKEN_PROGRAM))

    debt_pos = debt_position_pda(MARGIN, Pubkey.from_string(sol.reserve), PROGRAM)[0]
    assert keys(groups) == [
        usdc.config_address, str(vault(usdc)),                       # collateral group [asset_config, vault]
        jito.config_address, str(vault(jito)),
        sol.config_address, sol.reserve, str(debt_pos),              # debt group [asset_config, reserve, debt_position]
    ]
    assert all(not m.is_signer and not m.is_writable for m in groups)


async def test_named_positions_are_skipped():
    sol, usdc = make_asset("SOL", 3), make_asset("USDC", 13)
    registry = FakeRegistry(sol, usdc)
    margin = margin_with(collateral=[3, 13], debt=[13])
    groups = await position_groups(CLIENT, MARGIN, margin, registry, {13}, 13)
    assert keys(groups)[0] == sol.config_address and len(groups) == 2  # only SOL's collateral group left


async def test_writable_groups_for_liquidation_settlement():
    sol, usdc = make_asset("SOL", 3), make_asset("USDC", 13)
    registry = FakeRegistry(sol, usdc)
    margin = margin_with(collateral=[3], debt=[13])
    groups = await position_groups(CLIENT, MARGIN, margin, registry, set(), None, writable=True)
    # [config(ro), vault(w)] + [config(ro), reserve(w), debt_position(w)]
    assert [m.is_writable for m in groups] == [False, True, False, True, True]


def test_oracle_tail_is_deduplicated_and_readonly():
    a, b = make_asset("SOL", 3), make_asset("USDC", 13)
    metas = oracle_account_metas([a, b, a])
    assert keys(metas).count(SCOPE) == 1  # the shared Scope account once
    assert len(metas) == 1 + 2  # scope + one Pyth account each
    assert all(not m.is_writable and not m.is_signer for m in metas)


def test_oracle_tail_repeats_keys_the_instruction_already_names():
    # The oracle agent reads only its own segment, so a key that also appears among the call's accounts is repeated.
    receipt = make_asset("kSOL", 300, scope=False, pyth=False, klend=True, has_reserve=False)
    assert keys(oracle_account_metas([receipt])) == [receipt.oracle.klend_reserve]


async def test_health_accounts_include_oracles_of_named_and_active_assets():
    sol, usdc, jup = make_asset("SOL", 3), make_asset("USDC", 13), make_asset("JupSOL", 224, factor=True)
    registry = FakeRegistry(sol, usdc, jup)
    margin = margin_with(collateral=[3], debt=[])
    # Borrowing USDC: not active yet, named -> its oracle accounts must still be supplied.
    accounts = await build_health_accounts(
        CLIENT, MARGIN, margin, registry, exclude_collateral_indexes={13}, exclude_debt_index=13, extra_assets=[usdc]
    )
    tail = set(keys(accounts))
    for asset in (sol, usdc):
        assert set(asset.oracle.accounts()) <= tail
    assert not set(jup.oracle.accounts()) - {SCOPE} <= tail  # JupSOL isn't involved -> its Pyth accounts absent
    oracle_at = keys(accounts).index(ORACLE_PROGRAM)  # the oracle segment starts [vanna_oracle, price_book, ...]
    assert keys(accounts)[oracle_at + 1] == str(price_book_pda(Pubkey.from_string(ORACLE_PROGRAM))[0])
    assert keys(accounts)[:2] == [sol.config_address, str(find_associated_token_address(MARGIN, Pubkey.from_string(sol.mint), Pubkey.from_string(TOKEN_PROGRAM)))]


# --- dev-fork refresh helpers ------------------------------------------------------------

def test_known_pyth_feed_accounts_match_the_backend_keeper_output():
    # Addresses observed from the backend's own price keeper run against this fork.
    by_name = {f.name: str(f.account) for f in refresh._FEEDS}
    assert by_name["wsol"] == "7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE"
    assert by_name["usdc"] == "Dpw1EAVrSB1ibxiDQyTAW6Zip3J4Btk2x4SgApQCeFbX"
    assert by_name["usdt"] == "HT2PLQBcG5EiCcNSaMHAjSgd9F98ecpATbk4Sk5oYuM"


def test_fabricated_pyth_account_is_readable_by_the_pricing_port():
    template = bytearray(134)
    template[:8] = refresh._DISCRIMINATOR
    now = 1_800_000_000
    data = refresh._build_price_account_data(bytes(template), refresh._FEEDS[2].feed_id, 118.5, now)
    quote = pr.read_pyth(pr.AccountData(pr.PYTH_RECEIVER_PROGRAM_ID, data), 200)
    assert quote.price.to_decimal_str() == "118.5"
    assert quote.publish_time == now and quote.confidence_ok
    assert quote.ema.to_decimal_str() == "118.5"  # EMA == price => the TWAP check can never trip


def test_restamp_scope_entries_touches_only_the_requested_entries():
    data = bytearray(pr.ORACLE_PRICES_LEN)
    data[:8] = pr.ORACLE_PRICES_DISCRIMINATOR
    for entry in (3, 4, 9):
        at = 40 + 56 * entry
        data[at : at + 8] = (5).to_bytes(8, "little")           # value
        data[at + 24 : at + 32] = (111).to_bytes(8, "little")   # old timestamp
    out = refresh.restamp_scope_entries(bytes(data), {3, 4}, 999)
    ts = lambda entry: int.from_bytes(out[40 + 56 * entry + 24 : 40 + 56 * entry + 32], "little")  # noqa: E731
    assert (ts(3), ts(4), ts(9)) == (999, 999, 111)
    assert out[:40 + 56 * 3 + 24] == bytes(data)[:40 + 56 * 3 + 24]  # nothing else moved
    with pytest.raises(Exception):
        refresh.restamp_scope_entries(b"short", {1}, 1)


def test_scope_entries_used_covers_price_and_twap_chains():
    a = make_asset("SOL", 3)
    b = make_asset("JupSOL", 224, scope=False)  # Pyth-only: contributes nothing
    assert refresh.scope_entries_used([a, b]) == {3, 4}
