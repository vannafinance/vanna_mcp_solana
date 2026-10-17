"""What users actually say, end to end on the server side: each phrase is paired with the tool call a client model is
told to make for it (the tool descriptions and the routing table), and the server must turn that call into the right
plan with every number sized by the server. These are the prompts that broke in Codex / Cursor before the amount
vocabulary existed. No model is called here: which tool a model picks is tested by the user against real clients.

Prices in the fake chain: SOL $100, JitoSOL $110, USDC / USDT $1.
"""

from __future__ import annotations

import pytest

from mcp_server import safety
from mcp_server.dispatchers.plan import vanna_plan
from mcp_server.dispatchers.risk_advisor import vanna_risk
from mcp_server.oauth.store import MemoryRecordStore
from mcp_server.planning.engine import set_engine
from mcp_server.safety import Safety
from tests._fakechain import FakeChain, install
from vanna_core.config import get_settings

WALLET = "4FYMXgPzrvGQxKmbTTkZXfZABKTeUFTuDhyi8W73ETUL"


@pytest.fixture(autouse=True)
def _state():
    get_settings.cache_clear()
    safety.set_safety(Safety(MemoryRecordStore()))
    set_engine(None)  # previews: no signing page needed to check the drawn plan
    yield
    safety.set_safety(None)
    get_settings.cache_clear()


def chain(monkeypatch, **kw) -> FakeChain:
    return install(monkeypatch, FakeChain(wallet=WALLET, **kw))


def ops(out: dict) -> list[tuple[str, str | None, str | None]]:
    return [(s["op"], s.get("asset"), s.get("amount")) for s in out["steps"]]


# ------------------------------------------------------------------ deposit + borrow at a leverage: no swap


@pytest.mark.parametrize("phrase, call, expected", [
    ("take a loan of USDC with 3x leverage against depositing 10 sol",
     dict(action="leverage_borrow", asset="SOL", amount="10", leverage=3, borrow_asset="USDC"),
     [("deposit", "SOL", "10"), ("borrow", "USDC", "2000")]),
    ("deposit 10 sol and borrow usdc with 2x leverage against it",
     dict(action="leverage_borrow", asset="SOL", amount="10", leverage=2, borrow_asset="USDC"),
     [("deposit", "SOL", "10"), ("borrow", "USDC", "1000")]),
    ("take a loan of usdc with 2x leverage against depositing 1 jitoSOL",
     dict(action="leverage_borrow", asset="JitoSOL", amount="1", leverage=2, borrow_asset="USDC"),
     [("deposit", "JitoSOL", "1"), ("borrow", "USDC", "110")]),
    ("borrow USDT at 2x against my SOL",
     dict(action="leverage_borrow", asset="SOL", leverage=2, borrow_asset="USDT"),
     [("borrow", "USDT", "1000")]),
])
async def test_deposit_and_borrow_phrases_never_swap(monkeypatch, phrase, call, expected) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={"USDC": 0.0, "JitoSOL": 5.0}, sol_balance=50.0)
    out = await vanna_plan(authority=WALLET, **call)
    assert out["ok"], (phrase, out)
    writes = [(o, a, str(float(m))) for o, a, m in ops(out) if o != "report"]
    assert writes == [(o, a, str(float(m))) for o, a, m in expected], phrase
    assert not any(s["op"] == "swap" for s in out["steps"]), phrase
    assert out["simulation"]["hf_after"] >= 1.15


# ------------------------------------------------------------------ lever an asset on its own: a leveraged long (swap)


@pytest.mark.parametrize("phrase, call", [
    ("lever my SOL 2x", dict(action="lever", asset="SOL", leverage=2, borrow_asset="USDC")),
    ("go 3x long SOL with 5 SOL", dict(action="lever", asset="SOL", amount="5", leverage=3, borrow_asset="USDT")),
])
async def test_lever_alone_is_a_leveraged_long_with_a_swap(monkeypatch, phrase, call) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, sol_balance=50.0)
    out = await vanna_plan(authority=WALLET, **call)
    assert out["ok"], (phrase, out)
    kinds = [o for o, _, _ in ops(out)]
    assert kinds[-3:] == ["borrow", "swap", "report"], phrase
    swap = next(s for s in out["steps"] if s["op"] == "swap")
    assert swap["slots"]["to_asset"] == "SOL" and swap["slots"]["mode"] == "margin"


async def test_a_custom_long_can_also_be_composed_with_the_vocabulary(monkeypatch) -> None:
    """The same long built step by step: deposit -> borrow leverage:3 -> swap previous."""
    chain(monkeypatch, collateral={}, sol_balance=20.0)
    out = await vanna_plan(action="custom", authority=WALLET, steps=[
        {"op": "deposit", "asset": "SOL", "amount": "5"},
        {"op": "borrow", "asset": "USDC", "amount": "leverage:3"},
        {"op": "swap", "asset": "USDC", "to_asset": "SOL", "amount": "previous", "source": "margin"},
        {"op": "report"},
    ])
    assert out["ok"], out
    assert ops(out)[:3] == [("deposit", "SOL", "5"), ("borrow", "USDC", "1000"), ("swap", "USDC", "1000")]


# ------------------------------------------------------------------ the rest of the vocabulary


@pytest.mark.parametrize("phrase, step, expected_amount", [
    ("borrow $500 of USDC", {"op": "borrow", "asset": "USDC", "amount": "usd:500"}, "500"),
    ("deposit $250 worth of SOL", {"op": "deposit", "asset": "SOL", "amount": "$250"}, "2.5"),
    ("borrow as much USDC as is safe", {"op": "borrow", "asset": "USDC", "amount": "max_safe"}, "all"),
    ("withdraw half my SOL", {"op": "withdraw", "asset": "SOL", "amount": "50%"}, "5"),
    ("borrow USDC until my health factor is 2", {"op": "borrow", "asset": "USDC", "amount": "health:2"}, "200"),
    ("repay USDC from my wallet to get my health factor to 4", {"op": "repay", "asset": "USDC", "amount": "health:4", "source": "wallet"}, "150"),
])
async def test_amount_words_are_sized_by_the_server(monkeypatch, phrase, step, expected_amount) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 400.0}, wallet_tokens={"USDC": 1000.0}, sol_balance=50.0)
    out = await vanna_plan(action="custom", authority=WALLET, steps=[step])
    assert out["ok"], (phrase, out)
    got = out["steps"][0]["amount"]
    assert got == expected_amount if expected_amount == "all" else float(got) == pytest.approx(float(expected_amount)), (phrase, got)


async def test_previous_borrows_then_supplies_the_same_amount(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10})
    out = await vanna_plan(action="custom", authority=WALLET, steps=[
        {"op": "borrow", "asset": "USDC", "amount": "100"}, {"op": "earn_supply", "asset": "USDC", "amount": "previous"}])
    assert out["ok"] and ops(out) == [("borrow", "USDC", "100"), ("earn_supply", "USDC", "100")]


async def test_a_custom_plan_now_reports_its_health_factor(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, sol_balance=50.0)
    out = await vanna_plan(action="custom", authority=WALLET, steps=[
        {"op": "deposit", "asset": "SOL", "amount": "10"}, {"op": "borrow", "asset": "USDC", "amount": "1000"}])
    assert out["simulation"]["hf_before"] is None and out["simulation"]["hf_after"] == pytest.approx(3000 / 1000)


@pytest.mark.parametrize("step, text", [
    ({"op": "deposit", "asset": "SOL", "amount": "leverage:3"}, "sizes a borrow"),
    ({"op": "borrow", "asset": "USDC", "amount": "leverage:20"}, "10x"),
    ({"op": "borrow", "asset": "USDC", "amount": "health:1.05"}, "floor"),
    ({"op": "deposit", "asset": "SOL", "amount": "out:5"}, "swaps only"),
    ({"op": "earn_supply", "asset": "USDC", "amount": "previous"}, "earlier step"),
    ({"op": "borrow", "asset": "USDC", "amount": "lots"}, ""),
])
async def test_words_that_cannot_apply_are_refused_plainly(monkeypatch, step, text) -> None:
    chain(monkeypatch, collateral={"SOL": 10})
    out = await vanna_plan(action="custom", authority=WALLET, steps=[step])
    assert out["ok"] is False and text in out["message"], out


# ------------------------------------------------------------------ capacity with a big account and a small pool


async def test_capacity_is_capped_by_the_pool_not_reported_as_zero(monkeypatch) -> None:
    """The bug users hit: lots of collateral, a pool smaller than the health headroom -> it said 0 USDC."""
    chain(monkeypatch, collateral={"SOL": 1000}, debt={"USDC": 1000.0}, pool_liquidity={"SOL": 1e6, "USDC": 20_000.0, "USDT": 1e6})
    out = await vanna_risk(action="capacity", authority=WALLET, what="borrow", asset="USDC")
    row = out["capacity"][0]
    assert float(row["safe_amount"]) == pytest.approx(20_000 * 0.99, rel=1e-3)
    assert "pool liquidity" in row["limited_by"]


async def test_max_leverage_answers_for_both_kinds(monkeypatch) -> None:
    chain(monkeypatch, collateral={}, sol_balance=50.0)
    out = await vanna_risk(action="max_leverage", authority=WALLET, asset="SOL", amount="10")
    # deposit + borrow: (C + B) / B >= 1.15 on a 10 SOL ($1,000) deposit -> B <= 6,666.67 -> 7.6x (rounded down)
    assert out["max_leverage_deposit_borrow"] == pytest.approx(7.6)
    # lever (`max_leverage`, its meaning unchanged): the swap's worst price (1% slippage) leaves less room
    assert 1 < out["max_leverage"] < out["max_leverage_deposit_borrow"]
    three = next(r for r in out["table"] if r["leverage"] == 3)
    assert three["deposit_borrow_hf"] == pytest.approx(3000 / 2000) and three["worst_case_hf"] < three["deposit_borrow_hf"]


# ------------------------------------------------------------------ Predict: "sell all"


async def test_predict_sell_all_resolves_the_held_shares(monkeypatch) -> None:
    from vanna_core.contracts import predict as predict_mod

    market = "So11111111111111111111111111111111111111112"
    held = {"positions": [{"market_id": market, "side": "yes", "outcome": "Republican", "shares": 2.540682}],
            "can_move_to_pool": [{"market_id": market, "outcome": "yes", "shares": 1.5}]}

    class Fake(predict_mod.PredictContract):
        def __init__(self):
            pass

        async def positions(self, owner):
            return held

    client = Fake()
    legs = await client.resolve_relative_amounts(None, [{"market": market, "outcome": "yes", "side": "sell", "amount": "all"}])
    assert legs[0]["amount"] == "2.540682"
    legs = await client.resolve_relative_amounts(None, [{"market": market, "outcome": "yes", "side": "sell", "amount": "50%"}])
    assert legs[0]["amount"] == "1.270341"
    legs = await client.resolve_relative_amounts(None, [{"market": market, "outcome": "yes", "side": "deposit", "amount": "all"}])
    assert legs[0]["amount"] == "1.5"
    with pytest.raises(ValueError, match="CASH"):
        await client.resolve_relative_amounts(None, [{"market": market, "outcome": "yes", "side": "buy", "amount": "all"}])
    with pytest.raises(ValueError, match="holds no NO"):
        await client.resolve_relative_amounts(None, [{"market": market, "outcome": "no", "side": "sell", "amount": "all"}])
