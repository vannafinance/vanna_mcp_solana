"""Offline tests for sizing, pre-flight, strategies, the direct-write guard and the plan engine, against FakeChain."""

from __future__ import annotations

import base64

import pytest
from solders.keypair import Keypair

from mcp_server import safety
from mcp_server.oauth.store import MemoryRecordStore
from mcp_server.planning import engine as engine_mod
from mcp_server.planning import facts, strategies
from mcp_server.planning.engine import PlanEngine, PlanError, approval_message
from mcp_server.planning.funding import check_funding
from mcp_server.planning.guard import check_amounts, guard_direct_write, step_from_call
from mcp_server.planning.preflight import OUT_PREFIX, preflight_step, resolve_percent, size_exact_out, validate_steps
from mcp_server.safety import Safety
from mcp_server.signing import SendError
from tests._fakechain import FakeChain, install, sign_b64, sign_wrong_key
from vanna_core.config import get_settings

KP = Keypair()
WALLET = str(KP.pubkey())


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    safety.set_safety(Safety(MemoryRecordStore()))
    yield
    safety.set_safety(None)
    get_settings.cache_clear()


def chain(monkeypatch: pytest.MonkeyPatch, **kw) -> FakeChain:
    return install(monkeypatch, FakeChain(wallet=WALLET, **kw))


# ================================================================ facts / sizing


async def test_load_health_reads_strictly(monkeypatch) -> None:
    c = chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 400})
    h = await facts.load_health(WALLET)
    assert h.kind == "ok" and h.health.collateral_usd == pytest.approx(1000) and h.health.hf == pytest.approx(2.5)
    c.has_account = False
    assert (await facts.load_health(WALLET)).kind == "no_account"


async def test_safe_borrow_stays_at_or_above_the_floor_and_is_verified_by_the_program(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10})  # $1000, no debt
    s = await facts.size_safe_borrow(WALLET, "USDC")
    assert s.ok and s.hf_after >= 1.15 and s.hf_after < 1.16  # right at the floor, never below
    assert float(s.amount) == pytest.approx(1000 / 0.15 * 0.995, rel=0.001)  # (C - h*D)/(h-1), less a 0.5% margin
    # The program's own (looser) maximum is reported for reference only.
    assert s.protocol_max is not None and float(s.protocol_max) > float(s.amount)


async def test_safe_borrow_refuses_when_there_is_no_room(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 900})
    s = await facts.size_safe_borrow(WALLET, "USDC")
    assert not s.ok and "no room" in s.reason


async def test_safe_withdraw_limits_to_the_floor(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 500})  # C=1000 D=500 -> max withdraw = 1000-1.15*500 = 425 USD
    s = await facts.size_safe_withdraw(WALLET, "SOL")
    assert s.ok and float(s.amount) * 100 <= 425 and s.hf_after >= 1.15
    # With no debt everything may leave (the exact balance string is used).
    chain(monkeypatch, collateral={"SOL": 10})
    assert (await facts.size_safe_withdraw(WALLET, "SOL")).amount == "10"


async def test_wallet_spendable_keeps_a_sol_fee_reserve(monkeypatch) -> None:
    chain(monkeypatch, sol_balance=1.0, wallet_tokens={"USDC": 50.0})
    assert await facts.wallet_spendable(WALLET, "SOL") == pytest.approx(0.95)
    assert await facts.wallet_spendable(WALLET, "USDC") == 50.0
    assert await facts.wallet_spendable(WALLET, "USDT") is None


# ================================================================ exact-out + percent


async def test_exact_out_sizes_the_input_so_the_minimum_received_covers_the_target(monkeypatch) -> None:
    chain(monkeypatch)
    r = await size_exact_out(WALLET, "SOL", "USDC", 100.0, 100)
    assert r["ok"] and float(r["min_out"]) + 1e-12 >= 100.0
    assert float(r["amount_in"]) * 100 < 100 * 1.05  # not wildly oversized (within ~5% of the oracle amount)


async def test_exact_out_refuses_nonsense(monkeypatch) -> None:
    chain(monkeypatch)
    assert not (await size_exact_out(WALLET, "SOL", "USDC", 0.0, 100))["ok"]


async def test_resolve_percent_uses_live_figures(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 200}, wallet_tokens={"USDC": 80.0})
    assert (await resolve_percent(WALLET, "deposit", "USDC", 50))["amount"] == "40"
    assert (await resolve_percent(WALLET, "repay", "USDC", 25))["amount"] == "50"
    assert (await resolve_percent(WALLET, "withdraw", "SOL", 10))["amount"] == "1"
    r = await resolve_percent(WALLET, "deposit", "JitoSOL", 50)
    assert not r["ok"]


# ================================================================ lever


async def test_lever_plan_orders_deposit_borrow_swap_report_and_holds_the_floor(monkeypatch) -> None:
    chain(monkeypatch, collateral={}, wallet_tokens={"USDC": 0.0}, sol_balance=20.0)
    drawn = await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", "5")
    assert drawn["ok"], drawn
    ops = [(s["op"], s.get("asset")) for s in drawn["steps"]]
    assert ops == [("deposit", "SOL"), ("borrow", "USDC"), ("swap", "USDC"), ("report", None)]
    borrow = next(s for s in drawn["steps"] if s["op"] == "borrow")
    assert float(borrow["amount"]) == pytest.approx(500)  # (2-1) x 5 SOL x $100
    assert drawn["simulation"]["hf_after"] >= drawn["hf_floor"] == 1.15
    swap = next(s for s in drawn["steps"] if s["op"] == "swap")
    assert swap["slots"]["mode"] == "margin" and swap["slots"]["to_asset"] == "SOL"


async def test_lever_creates_the_account_first_when_there_is_none(monkeypatch) -> None:
    chain(monkeypatch, has_account=False, wallet_tokens={}, sol_balance=20.0)
    drawn = await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", "5")
    assert drawn["ok"] and drawn["steps"][0]["op"] == "create_account" and drawn["steps"][1]["op"] == "deposit"


async def test_lever_levers_the_existing_margin_balance_when_no_amount_is_given(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10})
    drawn = await strategies.plan_lever(WALLET, "SOL", 3.0, "USDC", None)
    assert drawn["ok"] and [s["op"] for s in drawn["steps"]] == ["borrow", "swap", "report"]
    assert float(drawn["steps"][0]["amount"]) == pytest.approx(2000)


async def test_lever_asks_which_stable_to_borrow(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10})
    r = await strategies.plan_lever(WALLET, "SOL", 2.0, None, None)
    assert not r["ok"] and {o["borrow_asset"] for o in r["options"]} == {"USDC", "USDT"}
    assert [o["borrow_asset"] for o in (await strategies.plan_lever(WALLET, "USDC", 2.0, None, None))["options"]] == ["USDT"]


async def test_lever_refuses_unsafe_leverage_and_offers_a_safe_one(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10})
    r = await strategies.plan_lever(WALLET, "SOL", 9.0, "USDC", None)
    assert not r["ok"] and "below the 1.15 floor" in r["message"] and r["options"]
    safe = r["options"][0]["leverage"]
    assert (await strategies.plan_lever(WALLET, "SOL", safe, "USDC", None))["ok"]


@pytest.mark.parametrize("kwargs, text", [
    (dict(asset="BONK", leverage=2.0, borrow_asset="USDC", amount=None), "not part of the product"),
    (dict(asset="SOL", leverage=1.0, borrow_asset="USDC", amount=None), "above 1x"),
    (dict(asset="SOL", leverage=11.0, borrow_asset="USDC", amount=None), "10x"),
    (dict(asset="SOL", leverage=2.0, borrow_asset="SOL", amount=None), "USDC or USDT"),
    (dict(asset="USDC", leverage=2.0, borrow_asset="USDC", amount=None), "same asset"),
    (dict(asset="SOL", leverage=2.0, borrow_asset="USDC", amount="-5"), "positive"),
    (dict(asset="SOL", leverage=2.0, borrow_asset="USDC", amount="1.0000000001"), "decimal"),
])
async def test_lever_guards(monkeypatch, kwargs, text) -> None:
    chain(monkeypatch, collateral={"SOL": 10})
    r = await strategies.plan_lever(WALLET, **kwargs)
    assert not r["ok"] and text in r["message"]


async def test_lever_refuses_when_the_wallet_cannot_fund_the_deposit(monkeypatch) -> None:
    chain(monkeypatch, sol_balance=1.0)
    r = await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", "5")
    assert not r["ok"] and "less than the" in r["message"] and "fees" in r["message"]


async def test_lever_respects_pool_liquidity(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, pool_liquidity={"SOL": 1e6, "USDC": 100.0, "USDT": 1e6})
    r = await strategies.plan_lever(WALLET, "SOL", 3.0, "USDC", None)
    assert not r["ok"] and "pool only has" in r["message"]


async def test_lever_min_hf_can_raise_the_floor_never_lower_it(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10})
    assert (await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", None, min_hf=1.01))["hf_floor"] == 1.15
    assert (await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", None, min_hf=1.4))["hf_floor"] == 1.4


# ================================================================ unwind


async def test_unwind_orders_farm_then_repay_then_withdraw_then_earn(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10, "USDC": 500.0}, debt={"USDC": 300.0}, farm={"SOL": 2.0}, earn_shares={"USDC": 40.0},
          wallet_tokens={})
    drawn = await strategies.plan_unwind(WALLET, "all", False)
    assert drawn["ok"], drawn
    ops = [(s["op"], s.get("asset"), (s.get("slots") or {}).get("source")) for s in drawn["steps"]]
    assert ops[0] == ("farm_redeem", "SOL", None)
    assert ("repay", "USDC", "margin") in ops
    assert ops.index(("repay", "USDC", "margin")) < min(i for i, o in enumerate(ops) if o[0] == "withdraw")
    assert ops[-1] == ("earn_redeem", "USDC", None)
    assert all(s["amount"] == "all" for s in drawn["steps"])


async def test_unwind_swaps_other_collateral_in_when_short_of_the_debt_asset(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 300.0}, wallet_tokens={})
    drawn = await strategies.plan_unwind(WALLET, "margin", False)
    assert drawn["ok"]
    swap = next(s for s in drawn["steps"] if s["op"] == "swap")
    assert swap["asset"] == "SOL" and swap["amount"].startswith(OUT_PREFIX) and swap["slots"]["mode"] == "margin"
    i = drawn["steps"].index(swap)
    assert drawn["steps"][i + 1]["op"] == "repay" and drawn["steps"][i + 1]["slots"]["source"] == "margin"


async def test_unwind_repays_from_the_wallet_when_it_can(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 300.0}, wallet_tokens={"USDC": 400.0})
    drawn = await strategies.plan_unwind(WALLET, "margin", False)
    assert next(s for s in drawn["steps"] if s["op"] == "repay")["slots"]["source"] == "wallet"


async def test_unwind_refuses_when_the_debt_cannot_be_repaid(monkeypatch) -> None:
    chain(monkeypatch, collateral={"JitoSOL": 0.1}, debt={"USDC": 900.0}, wallet_tokens={})
    r = await strategies.plan_unwind(WALLET, "margin", False)
    assert not r["ok"] and "can't be fully repaid" in r["message"]


async def test_unwind_with_nothing_open_says_so(monkeypatch) -> None:
    chain(monkeypatch, has_account=False)
    r = await strategies.plan_unwind(WALLET, "all", False)
    assert not r["ok"] and "no open positions" in r["message"]


async def test_unwind_caps_the_plan_and_never_cuts_between_a_swap_and_its_repay(monkeypatch) -> None:
    monkeypatch.setenv("MAX_PLAN_STEPS", "3")
    get_settings.cache_clear()
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 300.0}, wallet_tokens={})
    drawn = await strategies.plan_unwind(WALLET, "margin", False)
    assert drawn["ok"] and len(drawn["steps"]) <= 3
    assert drawn["steps"][-1]["op"] != "swap" or False
    assert any("first" in w for w in drawn["warnings"])


async def test_unwind_closes_the_account_only_when_asked(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 1.0})
    assert "close_account" not in [s["op"] for s in (await strategies.plan_unwind(WALLET, "margin", False))["steps"]]
    assert (await strategies.plan_unwind(WALLET, "margin", True))["steps"][-1]["op"] == "close_account"


# ================================================================ pre-flight


def step(op, asset=None, amount=None, **slots):
    return strategies.write_step(op, asset, amount, **slots)


async def test_borrow_is_refused_below_the_floor_even_when_the_program_would_allow_it(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 600.0})  # HF 1.67
    ok = await preflight_step(WALLET, step("borrow", "USDC", "100"))
    assert ok.ok and ok.prepared.args == {"action": "borrow", "authority": WALLET, "asset": "USDC", "amount": "100"}
    # 1000+ 300 / 600+300 = 1.44 fine; push to ~1.12: program says allowed (>1.10) but the product floor refuses it.
    refused = await preflight_step(WALLET, step("borrow", "USDC", "2700"))  # HF 1.12: the program allows it, the floor does not
    assert not refused.ok and refused.code == "risk" and "1.15" in refused.message


async def test_borrow_all_resolves_to_the_safe_maximum(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 5})
    r = await preflight_step(WALLET, step("borrow", "USDC", "all"))
    assert r.ok and float(r.prepared.resolved_amount) > 0 and r.prepared.risk.projected_health_factor >= 1.15


async def test_withdraw_and_swap_are_gated_but_deposit_and_repay_never_are(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 900.0})  # HF 1.11, unhealthy for the product
    assert not (await preflight_step(WALLET, step("withdraw", "SOL", "0.5"))).ok
    assert (await preflight_step(WALLET, step("deposit", "USDC", "100"))).ok
    assert (await preflight_step(WALLET, step("repay", "USDC", "100", source="wallet"))).ok
    risky_swap = await preflight_step(WALLET, step("swap", "SOL", "5", to_asset="USDC", mode="margin", slippage_bps=100))
    assert not risky_swap.ok and risky_swap.code == "risk"
    # A wallet swap never touches the margin account, so health does not gate it.
    assert (await preflight_step(WALLET, step("swap", "USDC", "100", to_asset="SOL", mode="wallet", slippage_bps=100))).ok


async def test_thin_health_asks_for_confirmation_rather_than_blocking(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 500.0})
    # leaves HF at (1000+x)/(500+x)... pick x so the result is in [1.15, 1.25)
    r = await preflight_step(WALLET, step("borrow", "USDC", "2000"))
    hf = (1000 + 2000) / (500 + 2000)
    assert 1.15 <= hf < 1.25 and r.ok and r.prepared.risk.decision == "needs_confirmation"


async def test_repay_all_uses_the_full_debt_and_says_interest_accrues(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 123.456})
    r = await preflight_step(WALLET, step("repay", "USDC", "all", source="margin"))
    assert r.ok and r.prepared.args["repay_all"] is True and "amount" not in r.prepared.args
    assert any("accrued" in n for n in r.prepared.notes)
    wallet_repay = await preflight_step(WALLET, step("repay", "USDC", "all", source="wallet"))
    assert wallet_repay.prepared.args["action"] == "repay_from_wallet" and wallet_repay.prepared.args["payer"] == WALLET


async def test_repay_with_no_debt_and_missing_account_are_clear(monkeypatch) -> None:
    c = chain(monkeypatch, collateral={"SOL": 10})
    assert (await preflight_step(WALLET, step("repay", "USDC", "5", source="wallet"))).code == "invalid"
    c.has_account = False
    assert (await preflight_step(WALLET, step("deposit", "SOL", "1"))).code == "no_account"


async def test_create_account_is_skipped_when_one_exists(monkeypatch) -> None:
    c = chain(monkeypatch)
    assert (await preflight_step(WALLET, step("create_account"))).skip
    c.has_account = False
    assert (await preflight_step(WALLET, step("create_account"))).prepared.args["action"] == "create"


async def test_close_account_requires_an_empty_account(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 1})
    assert (await preflight_step(WALLET, step("close_account"))).code == "invalid"


async def test_earn_redeem_all_burns_all_shares_and_partial_scales_shares(monkeypatch) -> None:
    chain(monkeypatch, earn_shares={"USDC": 100.0})
    full = await preflight_step(WALLET, step("earn_redeem", "USDC", "all"))
    assert full.prepared.args == {"action": "redeem", "owner": WALLET, "asset": "USDC", "shares": "100.0"}
    part = await preflight_step(WALLET, step("earn_redeem", "USDC", "25"))
    assert part.prepared.args["shares"] == "25"
    assert (await preflight_step(WALLET, step("earn_redeem", "USDT", "all"))).code == "invalid"


async def test_swap_exact_out_sizes_from_quotes_and_checks_the_balance(monkeypatch) -> None:
    chain(monkeypatch, wallet_tokens={"USDC": 0.0}, sol_balance=5.0)
    r = await preflight_step(WALLET, step("swap", "SOL", f"{OUT_PREFIX}100", to_asset="USDC", mode="wallet", slippage_bps=100))
    assert r.ok and float(r.prepared.resolved_amount) * 100 >= 100 and any("at least 100" in n for n in r.prepared.notes)
    poor = chain(monkeypatch, sol_balance=0.1)
    broke = await preflight_step(WALLET, step("swap", "SOL", f"{OUT_PREFIX}100", to_asset="USDC", mode="wallet", slippage_bps=100))
    assert not broke.ok and "needed" in broke.message


async def test_farm_deposit_and_redeem(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, farm={"SOL": 2.0})
    dep = await preflight_step(WALLET, step("farm_deposit", "SOL", "all"))
    assert dep.prepared.args == {"action": "deposit", "authority": WALLET, "underlying": "SOL", "amount": "10"}
    assert (await preflight_step(WALLET, step("farm_deposit", "SOL", "50"))).code == "invalid"
    red = await preflight_step(WALLET, step("farm_redeem", "SOL", "all"))
    assert red.prepared.args["action"] == "redeem" and any("receipt tokens" in n for n in red.prepared.notes)
    assert (await preflight_step(WALLET, step("farm_redeem", "USDC", "all"))).code == "invalid"


async def test_spend_caps_apply_per_action_and_per_day(monkeypatch) -> None:
    monkeypatch.setenv("MAX_ACTION_USD", "500")
    monkeypatch.setenv("MAX_DAILY_USD", "800")
    get_settings.cache_clear()
    chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={"USDC": 10_000.0})
    assert (await preflight_step(WALLET, step("deposit", "USDC", "600"))).code == "limit"  # above the per-action cap
    first = await preflight_step(WALLET, step("deposit", "USDC", "450"))
    assert first.ok
    await safety.get_safety().record_spend(WALLET, first.prepared.usd)
    second = await preflight_step(WALLET, step("deposit", "USDC", "450"))
    assert second.code == "limit" and "daily limit" in second.message


async def test_a_failed_price_read_refuses_instead_of_guessing(monkeypatch) -> None:
    c = chain(monkeypatch, collateral={"SOL": 10})
    async def broken(asset):
        return None
    monkeypatch.setattr(facts, "fresh_price", broken)
    r = await preflight_step(WALLET, step("deposit", "USDC", "5"))
    assert not r.ok and r.code == "unavailable"


# ================================================================ validation + funding + guard


def test_validate_steps_enforces_the_products_rules() -> None:
    ok = [step("deposit", "SOL", "1"), step("borrow", "USDC", "5")]
    assert validate_steps(ok) is None
    assert "no steps" in validate_steps([])
    assert "at most" in validate_steps([step("deposit", "SOL", "1")] * 9)
    assert "not part of the product" in validate_steps([step("deposit", "BONK", "1")])
    assert "no Vanna lending pool" in validate_steps([step("borrow", "JitoSOL", "1")])
    assert "no Vanna lending pool" in validate_steps([step("earn_supply", "JupSOL", "1")])
    assert "missing its amount" in validate_steps([step("deposit", "SOL", None)])
    assert "Unsupported" in validate_steps([step("liquidate", "SOL", "1")])
    assert "different assets" in validate_steps([step("swap", "SOL", "1", to_asset="SOL")])
    assert validate_steps([strategies.report_step()]) is None


async def test_funding_check_catches_a_short_wallet_before_anything_is_built(monkeypatch) -> None:
    chain(monkeypatch, wallet_tokens={"USDC": 50.0}, sol_balance=0.01)
    assert "fees and account rent" in await check_funding(WALLET, [step("deposit", "USDC", "10")])
    chain(monkeypatch, wallet_tokens={"USDC": 50.0}, sol_balance=5.0)
    assert "needs 100 USDC" in await check_funding(WALLET, [step("deposit", "USDC", "100")])
    assert await check_funding(WALLET, [step("deposit", "USDC", "40")]) is None
    # After a borrow/withdraw the wallet balance may change, so later steps are checked when their turn comes.
    assert await check_funding(WALLET, [step("borrow", "USDC", "5"), step("deposit", "USDC", "9999")]) is None


def test_direct_calls_map_to_the_same_steps_as_plans() -> None:
    s = step_from_call("vanna_earn_write", {"action": "supply", "owner": WALLET, "asset": "USDC", "amount": "5"})
    assert s["op"] == "earn_supply" and s["amount"] == "5"
    s = step_from_call("vanna_margin_trade", {"action": "repay", "authority": WALLET, "asset": "USDC", "repay_all": True})
    assert s["op"] == "repay" and s["amount"] == "all" and s["slots"]["source"] == "margin"
    s = step_from_call("vanna_swap", {"action": "margin_swap", "authority": WALLET, "from_asset": "SOL", "to_asset": "USDC", "amount": "1"})
    assert s["slots"]["mode"] == "margin"
    assert step_from_call("vanna_margin_trade", {"action": "deposit_and_borrow"}) is None
    # Repaying someone else's debt has no single-step plan equivalent.
    assert step_from_call("vanna_margin_trade", {"action": "repay_from_wallet", "authority": "x", "payer": "y", "asset": "USDC", "amount": "1"}) is None


@pytest.mark.parametrize("args", [
    {"action": "deposit", "asset": "USDC", "amount": "-1"}, {"action": "deposit", "asset": "USDC", "amount": "0"},
    {"action": "deposit", "asset": "USDC", "amount": "1.1234567"}, {"action": "deposit", "asset": "USDC", "amount": "NaN"},
    {"action": "deposit_and_borrow", "deposit_asset": "SOL", "deposit_amount": "1e3", "borrow_asset": "USDC", "borrow_amount": "5"},
])
def test_typed_amounts_are_refused_not_rounded(args) -> None:
    assert check_amounts("vanna_margin_trade", args)


async def test_guard_runs_the_same_preflight_for_direct_writes_and_builds_what_was_asked(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 600.0})
    args = {"action": "borrow", "authority": WALLET, "asset": "USDC", "amount": "2700"}
    refused = await guard_direct_write(WALLET, "vanna_margin_trade", args)
    assert not refused.ok and refused.code == "risk"
    fine = {"action": "borrow", "authority": WALLET, "asset": "USDC", "amount": "100"}
    ok = await guard_direct_write(WALLET, "vanna_margin_trade", fine)
    assert ok.ok and ok.prepared.args == fine
    # A composite with no plan equivalent still gets the limits check.
    assert (await guard_direct_write(WALLET, "vanna_margin_trade", {"action": "close_position", "authority": WALLET})).ok


# ================================================================ engine


class Net:
    """Sender/status reader for the engine."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail: Exception | None = None
        self.confirmed = True

    async def send(self, signed_b64: str) -> str:
        if self.fail:
            raise self.fail
        self.sent.append(signed_b64)
        return "5" * 87

    async def statuses(self, sigs):
        return [{"err": None, "confirmationStatus": "confirmed"} if self.confirmed else None for _ in sigs]


def make_engine(net: Net, clock=None) -> PlanEngine:
    kwargs = {"clock": clock} if clock else {}
    return PlanEngine(MemoryRecordStore(), safety.get_safety(), net.send, net.statuses, lambda: "http://127.0.0.1:8000", **kwargs)


def approval_sig(view_or_rec, kp=KP) -> str:
    msg = view_or_rec["approval_message"]
    return base64.b64encode(bytes(kp.sign_message(msg.encode()))).decode()


async def drawn_lever(monkeypatch):
    chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={}, sol_balance=5.0)
    return await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", None)


async def test_engine_runs_a_plan_end_to_end_exactly_once(monkeypatch) -> None:
    net, c = Net(), chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={}, sol_balance=5.0)
    eng = make_engine(net)
    view = await eng.create(WALLET, await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", None), kind="lever")
    assert view["status"] == "awaiting_approval" and view["plan_url"].endswith(view["plan_id"]) and len(view["digest"]) == 64
    pid = view["plan_id"]

    with pytest.raises(PlanError) as e:  # nothing is built before the wallet approves
        await eng.build_next(pid)
    assert e.value.code == "not_approved"

    approved = await eng.approve(pid, WALLET, approval_sig(view))
    assert approved["status"] == "approved"

    built = await eng.build_next(pid)  # step 1: borrow
    assert built["ok"] and built["label"].startswith("Borrow") and built["transactions"]
    tx = built["transactions"][0]
    signed = sign_b64(tx["unsignedTransaction"], KP)
    sub = await eng.submit(pid, tx["id"], signed)
    assert sub["signature"] and len(net.sent) == 1

    with pytest.raises(PlanError) as again:  # exactly once: the same step cannot be sent twice
        await eng.submit(pid, tx["id"], signed)
    assert again.value.code == "not_built"

    nxt = await eng.build_next(pid)  # step 2: swap (the borrow confirmed)
    assert nxt["ok"] and nxt["label"].startswith("Swap")
    tx2 = nxt["transactions"][0]
    await eng.submit(pid, tx2["id"], sign_b64(tx2["unsignedTransaction"], KP))

    done = await eng.build_next(pid)  # step 3: the closing health report runs inline
    assert done["done"] and done["report"] is not None
    final = await eng.get(pid)
    assert final["status"] == "completed" and [s["state"] for s in final["steps"]] == ["confirmed"] * 3


async def test_approval_needs_the_plans_own_wallet_and_a_valid_signature(monkeypatch) -> None:
    eng = make_engine(Net())
    view = await eng.create(WALLET, await drawn_lever(monkeypatch), kind="lever")
    other = Keypair()
    with pytest.raises(PlanError) as e:
        await eng.approve(view["plan_id"], str(other.pubkey()), approval_sig(view, other))
    assert e.value.code == "wrong_wallet"
    with pytest.raises(PlanError) as e2:  # a signature over some other message
        await eng.approve(view["plan_id"], WALLET, base64.b64encode(bytes(KP.sign_message(b"hello"))).decode())
    assert e2.value.code == "invalid_signature"
    with pytest.raises(PlanError):
        await eng.approve(view["plan_id"], WALLET, "not-base64!!")


async def test_an_approval_is_tied_to_the_digest_so_a_tampered_plan_cannot_ride_it(monkeypatch) -> None:
    eng = make_engine(Net())
    view = await eng.create(WALLET, await drawn_lever(monkeypatch), kind="lever")
    sig = approval_sig(view)
    rec = await eng.store.get(f"plan:{view['plan_id']}")
    rec["steps"][0]["amount"] = "99999"  # somebody edits the stored plan after the user saw it
    rec["digest"] = "0" * 64
    await eng.store.put(f"plan:{view['plan_id']}", rec, 100)
    with pytest.raises(PlanError) as e:
        await eng.approve(view["plan_id"], WALLET, sig)
    assert e.value.code == "invalid_signature"  # the signed message named the original digest


async def test_a_plan_not_approved_in_time_expires(monkeypatch) -> None:
    now = [1_000_000.0]
    eng = make_engine(Net(), clock=lambda: now[0])
    view = await eng.create(WALLET, await drawn_lever(monkeypatch), kind="lever")
    now[0] += 301
    with pytest.raises(PlanError) as e:
        await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    assert e.value.code == "expired"
    assert (await eng.get(view["plan_id"]))["status"] == "expired"


async def test_the_run_window_closes(monkeypatch) -> None:
    now = [1_000_000.0]
    eng = make_engine(Net(), clock=lambda: now[0])
    view = await eng.create(WALLET, await drawn_lever(monkeypatch), kind="lever")
    await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    now[0] += 1801
    with pytest.raises(PlanError) as e:
        await eng.build_next(view["plan_id"])
    assert e.value.code == "expired"


async def test_a_signed_transaction_that_differs_from_what_was_built_is_refused(monkeypatch) -> None:
    net = Net()
    eng = make_engine(net)
    c = chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={}, sol_balance=5.0)
    view = await eng.create(WALLET, await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", None), kind="lever")
    await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    built = await eng.build_next(view["plan_id"])
    tx = built["transactions"][0]
    from tests._fakechain import unsigned_tx_b64

    other_message = sign_b64(unsigned_tx_b64(WALLET, "different instruction"), KP)  # validly signed, but not what was prepared
    with pytest.raises(PlanError) as e:
        await eng.submit(view["plan_id"], tx["id"], other_message)
    assert e.value.code == "transaction_changed" and not net.sent
    stranger = sign_wrong_key(tx["unsignedTransaction"], Keypair())  # signed by the wrong key
    with pytest.raises(PlanError) as e2:
        await eng.submit(view["plan_id"], tx["id"], stranger)
    assert e2.value.code in ("invalid_signature", "transaction_changed") and not net.sent
    with pytest.raises(PlanError) as e3:  # unsigned
        await eng.submit(view["plan_id"], tx["id"], tx["unsignedTransaction"])
    assert e3.value.code == "invalid_signature" and not net.sent
    with pytest.raises(PlanError) as e4:
        await eng.submit(view["plan_id"], tx["id"], base64.b64encode(b"garbage").decode())
    assert e4.value.code == "invalid_transaction"


async def test_a_step_that_would_breach_the_floor_pauses_the_plan(monkeypatch) -> None:
    eng = make_engine(Net())
    c = chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 500.0}, wallet_tokens={}, sol_balance=5.0)
    drawn = {"summary": "Borrow 100 USDC", "steps": [step("borrow", "USDC", "100")], "warnings": [], "simulation": None, "hf_floor": 1.15}
    view = await eng.create(WALLET, drawn, kind="custom")
    await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    c.prices["SOL"] = 57.0  # the price falls between planning and the step: the borrow would now leave HF ~1.12 (program allows, floor refuses)
    r = await eng.build_next(view["plan_id"])
    assert r["ok"] is False and r["code"] == "risk"
    assert (await eng.get(view["plan_id"]))["status"] == "paused"
    c.prices["SOL"] = 100.0  # recovered: the same step can now be built
    assert (await eng.build_next(view["plan_id"]))["ok"]


async def test_a_failed_simulation_is_reported_and_nothing_is_handed_to_the_wallet(monkeypatch) -> None:
    eng = make_engine(Net())
    c = chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={}, sol_balance=5.0)
    view = await eng.create(WALLET, await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", None), kind="lever")
    await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    c.simulation_ok = False
    r = await eng.build_next(view["plan_id"])
    assert r["ok"] is False and "slippage" in r["reason"] and "transactions" not in r


async def test_an_explicit_network_rejection_lets_the_step_be_retried_but_an_unclear_outcome_never_is(monkeypatch) -> None:
    net = Net()
    eng = make_engine(net)
    chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={}, sol_balance=5.0)
    view = await eng.create(WALLET, await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", None), kind="lever")
    await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    tx = (await eng.build_next(view["plan_id"]))["transactions"][0]
    signed = sign_b64(tx["unsignedTransaction"], KP)
    net.fail = SendError("blockhash not found")
    with pytest.raises(PlanError) as e:
        await eng.submit(view["plan_id"], tx["id"], signed)
    assert e.value.code == "send_failed"
    net.fail = None
    assert (await eng.submit(view["plan_id"], tx["id"], signed))["signature"]  # it never left: retry is safe

    net2 = Net()
    eng2 = make_engine(net2)
    view2 = await eng2.create(WALLET, await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", None), kind="lever")
    await eng2.approve(view2["plan_id"], WALLET, approval_sig(view2))
    tx2 = (await eng2.build_next(view2["plan_id"]))["transactions"][0]
    net2.fail = TimeoutError("network gone")
    with pytest.raises(PlanError) as e2:
        await eng2.submit(view2["plan_id"], tx2["id"], sign_b64(tx2["unsignedTransaction"], KP))
    assert e2.value.code == "uncertain"
    final = await eng2.get(view2["plan_id"])
    assert final["status"] == "failed" and final["steps"][0]["state"] == "uncertain"
    with pytest.raises(PlanError):
        await eng2.build_next(view2["plan_id"])  # never retried automatically


async def test_a_transaction_that_never_lands_expires_and_is_rebuilt(monkeypatch) -> None:
    now = [1_000_000.0]
    net = Net()
    net.confirmed = False
    eng = make_engine(net, clock=lambda: now[0])
    chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={}, sol_balance=5.0)
    view = await eng.create(WALLET, await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", None), kind="lever")
    await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    tx = (await eng.build_next(view["plan_id"]))["transactions"][0]
    await eng.submit(view["plan_id"], tx["id"], sign_b64(tx["unsignedTransaction"], KP))
    assert (await eng.build_next(view["plan_id"]))["waiting"]  # not yet confirmed: do not build the next step
    now[0] += 200
    assert (await eng.get(view["plan_id"]))["steps"][0]["state"] == "expired"
    assert (await eng.build_next(view["plan_id"]))["ok"]  # rebuilt from fresh data


async def test_onchain_failure_stops_the_plan(monkeypatch) -> None:
    net = Net()
    eng = make_engine(net)
    chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={}, sol_balance=5.0)
    view = await eng.create(WALLET, await strategies.plan_lever(WALLET, "SOL", 2.0, "USDC", None), kind="lever")
    await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    tx = (await eng.build_next(view["plan_id"]))["transactions"][0]
    await eng.submit(view["plan_id"], tx["id"], sign_b64(tx["unsignedTransaction"], KP))
    async def failing(sigs):
        return [{"err": {"InstructionError": [0, "Custom"]}, "confirmationStatus": "confirmed"} for _ in sigs]
    eng._statuses = failing
    final = await eng.get(view["plan_id"])
    assert final["status"] == "failed" and final["steps"][0]["state"] == "failed"
    with pytest.raises(PlanError):
        await eng.build_next(view["plan_id"])


async def test_cancel_stops_a_plan_and_a_cancelled_plan_cannot_run(monkeypatch) -> None:
    eng = make_engine(Net())
    view = await eng.create(WALLET, await drawn_lever(monkeypatch), kind="lever")
    await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    assert (await eng.cancel(view["plan_id"], WALLET))["status"] == "cancelled"
    with pytest.raises(PlanError):
        await eng.build_next(view["plan_id"])
    with pytest.raises(PlanError) as e:
        await eng.cancel(view["plan_id"], str(Keypair().pubkey()))  # not your plan
    assert e.value.code == "not_found"


async def test_status_is_visible_only_to_the_plans_wallet(monkeypatch) -> None:
    eng = make_engine(Net())
    view = await eng.create(WALLET, await drawn_lever(monkeypatch), kind="lever")
    assert (await eng.status_for(WALLET, view["plan_id"]))["wallet"] == WALLET
    with pytest.raises(PlanError):
        await eng.status_for(str(Keypair().pubkey()), view["plan_id"])
    with pytest.raises(PlanError):
        await eng.status_for(None, "short")


async def test_two_browsers_cannot_build_the_same_step_at_once(monkeypatch) -> None:
    eng = make_engine(Net())
    view = await eng.create(WALLET, await drawn_lever(monkeypatch), kind="lever")
    await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    await eng.store.put(f"planlock:{view['plan_id']}:0", {"at": 1}, 30)  # another request holds the step
    with pytest.raises(PlanError) as e:
        await eng.build_next(view["plan_id"])
    assert e.value.code == "busy"


async def test_the_digest_changes_with_any_executable_slot_but_not_with_wording() -> None:
    steps = [step("borrow", "USDC", "100")]
    base = engine_mod.plan_digest(WALLET, 1.15, steps)
    assert engine_mod.plan_digest(WALLET, 1.15, [step("borrow", "USDC", "101")]) != base
    assert engine_mod.plan_digest(WALLET, 1.2, steps) != base
    assert engine_mod.plan_digest(str(Keypair().pubkey()), 1.15, steps) != base
    assert engine_mod.plan_digest(WALLET, 1.15, [{**steps[0], "label": "different words"}]) == base


async def test_a_direct_write_is_a_one_step_plan(monkeypatch) -> None:
    net = Net()
    eng = make_engine(net)
    chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={"USDC": 500.0})
    view = await eng.create_from_call(WALLET, "vanna_earn_write", {"action": "supply", "owner": WALLET, "asset": "USDC", "amount": "100"},
                                      "Supply 100 USDC to the Vanna Earn pool")
    assert view["kind"] == "single" and len(view["steps"]) == 1
    await eng.approve(view["plan_id"], WALLET, approval_sig(view))
    tx = (await eng.build_next(view["plan_id"]))["transactions"][0]
    await eng.submit(view["plan_id"], tx["id"], sign_b64(tx["unsignedTransaction"], KP))
    done = await eng.build_next(view["plan_id"])  # the one step confirmed: asking again just reports completion
    assert done["done"] and (await eng.get(view["plan_id"]))["status"] == "completed"


async def test_deposit_and_borrow_is_held_to_the_floor_and_the_limits(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 600.0}, wallet_tokens={"USDC": 0.0}, sol_balance=5.0)
    args = {"action": "deposit_and_borrow", "authority": WALLET, "deposit_asset": "SOL", "deposit_amount": "1",
            "borrow_asset": "USDC", "borrow_amount": "100"}
    ok = await guard_direct_write(WALLET, "vanna_margin_trade", args)
    assert ok.ok and ok.prepared.args == args
    # HF after = (1000 + 100 + b) / (600 + b); b = 3500 -> 4600/4100 = 1.12: allowed by the program, refused by the floor.
    risky = {**args, "borrow_amount": "3500"}
    refused = await guard_direct_write(WALLET, "vanna_margin_trade", risky)
    assert not refused.ok and refused.code == "risk" and "1.15" in refused.message
    assert (await guard_direct_write(WALLET, "vanna_margin_trade", {**args, "borrow_asset": "JitoSOL"})).code == "invalid"
    assert (await guard_direct_write(WALLET, "vanna_margin_trade", {**args, "deposit_asset": "BONK"})).code == "invalid"
    assert (await guard_direct_write(WALLET, "vanna_margin_trade", {**args, "borrow_amount": "-1"})).code == "invalid"


async def test_direct_writes_spend_the_build_budget_but_plan_steps_do_not_pay_twice(monkeypatch) -> None:
    monkeypatch.setenv("WRITE_RATE_PER_MINUTE", "3")
    get_settings.cache_clear()
    chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={"USDC": 1000.0})
    args = {"action": "deposit", "authority": WALLET, "asset": "USDC", "amount": "1"}
    results = [await guard_direct_write(WALLET, "vanna_margin_trade", args) for _ in range(4)]
    assert [r.ok for r in results] == [True, True, True, False] and results[3].code == "limit"
    # A raw plan step is guarded without consuming (the engine consumes once when it builds).
    raw = await guard_direct_write(WALLET, "vanna_margin_trade", {"action": "close_position", "authority": WALLET}, consume_rate=False)
    assert raw.ok
