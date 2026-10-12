"""`vanna_predict`: the app's Predict API mapped, trade legs checked, the maker-signed batch checked, and a batch run
through the plan engine (one approval, sent in order, merge after it confirms). Offline: the app is a MockTransport."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from mcp_server import internal, safety
from mcp_server.oauth.store import MemoryRecordStore
from mcp_server.planning.engine import PlanEngine, PlanError
from mcp_server.planning.guard import describe_call, predict_spend_usd
from mcp_server.safety import Safety
from vanna_core.config import get_settings
from vanna_core.contracts import predict as predict_mod
from vanna_core.contracts.predict import WORLD_PROGRAM_ID, PredictContract, validate_legs
from vanna_core.types import ContractCallError

OWNER = Keypair()
MAKER = Keypair()
MARKET = "6tutZgRrk3GNkaTNpcxaoHdj2CBtVZTe9YqxnipzEBUT"
MEMO = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
REAL_CLIENT = httpx.AsyncClient


def batch_tx(tag: str = "buy", *, payer: Keypair = OWNER, co: Keypair | None = MAKER, program: Pubkey | None = None,
             maker_signs: bool = True) -> str:
    accounts = [AccountMeta(payer.pubkey(), True, True)]
    if co is not None:
        accounts.append(AccountMeta(co.pubkey(), True, False))
    ix = Instruction(program or Pubkey.from_string(WORLD_PROGRAM_ID), tag.encode(), accounts)
    message = MessageV0.try_compile(payer.pubkey(), [ix], [], Hash.default())
    sigs = [Signature.default()]
    if co is not None:
        sigs.append(co.sign_message(to_bytes_versioned(message)) if maker_signs else Signature.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(message, sigs))).decode()


EVENT = {
    "id": "FzMSs3Df", "type": "match", "cat": "sports", "tag": "UCL", "title": "Galatasaray vs Barcelona", "last": "3m ago",
    "ends": "", "oi": 120.5, "v24": 30.0, "rules": "Resolves YES if Barcelona wins.", "exh": True,
    "outs": [{"marketId": MARKET, "k": MARKET, "name": "Barcelona", "y": 78.4, "n": 23.4, "yBid": 76.7, "nBid": 21.7,
              "tradable": True, "priceSource": "quote", "oi": 9.4, "vol": 1.2, "pool": {"yes": 3, "no": 2},
              "poolDetail": {"allowlisted": True, "invested": 2, "pendingMerge": 0}}],
}


def app(trade: dict | None = None, *, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path.removeprefix("/api/predict")
        if path == "/status":
            return httpx.Response(200, json={"maker": str(MAKER.pubkey())})
        if path == "/events":
            return httpx.Response(200, json={"events": [EVENT], "updatedAt": 1})
        if path.startswith("/events/"):
            return httpx.Response(200, json={"event": EVENT}) if path.endswith("FzMSs3Df") else httpx.Response(404, json={"error": "Event not found"})
        if path.startswith("/positions/"):
            return httpx.Response(200, json={
                "owner": str(OWNER.pubkey()), "wallet": {"cash": 990, "usdc": 5, "sol": 1.5},
                "positions": [{"event": "Galatasaray vs Barcelona", "eventId": "FzMSs3Df", "marketId": MARKET, "outcome": "Barcelona",
                               "side": "yes", "label": "Yes", "shares": 12.7, "walletShares": 0, "poolValueShares": 12.7,
                               "avg": 0.7835, "mark": 0.767, "value": 9.74, "cost": 10, "pnl": -0.26, "pays": 12.7, "apy": 1.6}],
                "summary": {"value": 9.74, "cost": 10, "pnl": -0.26, "payout": 12.7, "inVault": 0, "apy": 1.6, "count": 1},
                "vault": {"deposited": 33.2, "apy": 1.64, "pairs": 33.2, "events": 1},
                "importable": [{"event": "X", "marketId": MARKET, "side": "no", "label": "No", "shares": 4, "value": 0.9, "apy": 1.6, "listed": True}],
            })
        if path.startswith("/activity/"):
            return httpx.Response(200, json={"trades": [{"signature": "s", "time": 1_760_000_000_000, "marketId": MARKET, "question": "Q",
                                                         "outcome": "yes", "outcomeLabel": "Yes", "side": "buy", "shares": 1.27, "cash": 1, "price": 0.7835, "pooled": 1.27}]})
        if path == "/trade":
            body = json.loads(request.content)
            assert body["pay"] == "CASH" and "noPool" not in body
            return httpx.Response(200, json=trade or {
                "transactions": [{"transaction": batch_tx("buy"), "label": "Buy YES + Supply YES to the yield pool", "legIndexes": [0], "makerSigned": True}],
                "quotes": [{"legIndex": 0, "marketId": MARKET, "outcome": "yes", "side": "buy", "amountIn": 1, "amountOut": 1.276324,
                            "price": 0.7835, "payoutIfWin": 1.276324, "pooled": 1.276324, "redeemedShares": 0}],
                "lastValidBlockHeight": 2825258, "mergeMarkets": [MARKET], "swap": None, "warnings": [],
            })
        if path == "/pool/merge":
            return httpx.Response(200, json={"results": [{"marketId": MARKET, "signature": "m", "merged": 1.27}]})
        if path == "/faucet":
            return httpx.Response(200, json={"ok": True, "added": 100, "cash": 1100, "sol": 2})
        return httpx.Response(404, json={"error": "no route"})

    return handler


class Sim:
    def __init__(self, err=None) -> None:
        self.err = err
        self.calls = 0

    async def __call__(self, tx):
        self.calls += 1
        return SimpleNamespace(value=SimpleNamespace(err=self.err, logs=["log"], units_consumed=1000), context=SimpleNamespace(slot=7))


def contract(monkeypatch, handler, *, sim: Sim | None = None, cluster: str = "surfpool-fork") -> PredictContract:
    monkeypatch.setattr(predict_mod.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(predict_mod, "_maker_cache", None)
    settings = SimpleNamespace(predict_api_url="https://app.test/api/predict", perps_helper_url="", price_keeper_url="",
                               app_access_code="", solana_cluster_label=cluster)
    return PredictContract(SimpleNamespace(settings=settings, simulate_versioned_transaction=sim or Sim()))


# ================================================================ legs


@pytest.mark.parametrize("legs, message", [
    ([], "At least one leg"),
    ([{"market": MARKET, "outcome": "yes", "amount": "1"}] * 9, "At most 8"),
    ([{"market": "nope", "outcome": "yes", "amount": "1"}], "market id"),
    ([{"market": MARKET, "outcome": "maybe", "amount": "1"}], "yes or no"),
    ([{"market": MARKET, "outcome": "yes", "side": "short", "amount": "1"}], "buy, sell or deposit"),
    ([{"market": MARKET, "outcome": "yes", "amount": "0.05"}], "between 0.1 and 5,000"),
    ([{"market": MARKET, "outcome": "yes", "amount": "5000.5"}], "between 0.1 and 5,000"),
    ([{"market": MARKET, "outcome": "yes", "amount": "1.1234567"}], "more than 6 decimals"),
    ([{"market": MARKET, "outcome": "yes", "amount": "-1"}], "greater than 0"),
])
def test_legs_are_checked_like_the_app_before_any_call(legs, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_legs(legs)


def test_a_sell_or_pool_deposit_has_no_cash_minimum_and_legs_map_to_the_apps_request() -> None:
    legs = validate_legs([{"market": MARKET, "outcome": "NO", "side": "sell", "amount": "0.01"},
                          {"market_id": MARKET, "outcome": "yes", "side": "deposit", "amount": "4"}])
    assert legs == [{"marketId": MARKET, "outcome": "no", "side": "sell", "amount": 0.01},
                    {"marketId": MARKET, "outcome": "yes", "side": "deposit", "amount": 4.0}]


def test_limits_count_only_the_cash_buys_spend() -> None:
    args = {"action": "trade", "legs": [{"market": MARKET, "outcome": "yes", "amount": "10"},
                                        {"market": MARKET, "outcome": "no", "side": "sell", "amount": "50"}]}
    assert predict_spend_usd(args) == 10.0
    assert describe_call("vanna_predict", {"action": "trade", "market": MARKET, "outcome": "yes", "amount": "5"}) == "Buy 5 CASH of YES (Predict)"
    assert describe_call("vanna_predict", args) == "Predict trade on 2 outcomes (one approval)"


# ================================================================ reads


async def test_markets_market_positions_history_map_the_apps_figures(monkeypatch) -> None:
    calls: list = []
    p = contract(monkeypatch, app(calls=calls))
    listed = await p.markets("sports", " barca ")
    assert calls[-1].url.params["cat"] == "sports" and calls[-1].url.params["q"] == "barca"
    out = listed["events"][0]["outcomes"][0]
    assert out == {"market_id": MARKET, "name": "Barcelona", "yes_label": "Yes", "no_label": "No", "yes_buy_cents": 78.4,
                   "no_buy_cents": 23.4, "yes_sell_cents": 76.7, "no_sell_cents": 21.7, "tradable": True, "price_source": "quote",
                   "joins_vanna_pool": True, "open_interest_cash": 9.4, "volume_24h_cash": 1.2}
    one = (await p.market("FzMSs3Df"))["event"]
    assert one["rules"].startswith("Resolves") and one["outcomes"][0]["pool"]["usdc_lent_to_vanna"] == 2
    with pytest.raises(ContractCallError, match="Event not found"):
        await p.market("missing")
    pos = await p.positions(OWNER.pubkey())
    assert pos["wallet"]["cash"] == 990 and pos["positions"][0]["avg_price_cents"] == 78.35
    assert pos["can_move_to_pool"][0] == {"event": "X", "market_id": MARKET, "outcome": "no", "label": "No", "shares": 4,
                                          "value_usd": 0.9, "pool_apy_pct": 1.6}
    hist = await p.history(OWNER.pubkey())
    assert hist["trades"][0]["price_cents"] == 78.35 and hist["trades"][0]["time"].startswith("2025-10-09")
    with pytest.raises(ValueError, match="category"):
        await p.markets("weather")


async def test_a_quote_returns_no_transactions(monkeypatch) -> None:
    p = contract(monkeypatch, app())
    q = await p.quote(OWNER.pubkey(), [{"market": MARKET, "outcome": "yes", "amount": "1"}])
    assert q["quotes"][0] == {"leg": 1, "market_id": MARKET, "outcome": "yes", "side": "buy", "cash_in": 1, "shares_out": 1.276324,
                              "price_cents": 78.35, "payout_if_win_cash": 1.276324, "shares_into_vanna_pool": 1.276324,
                              "pool_shares_redeemed": 0}
    assert q["cash_spent"] == 1 and "transaction" not in json.dumps(q["quotes"])


async def test_the_apps_error_and_gate_are_reported_plainly(monkeypatch) -> None:
    p = contract(monkeypatch, lambda r: httpx.Response(400, json={"error": "Trade between 0.1 and 5000 CASH per leg."}))
    with pytest.raises(ContractCallError, match="Trade between"):
        await p.quote(OWNER.pubkey(), [{"market": MARKET, "outcome": "yes", "amount": "1"}])
    p = contract(monkeypatch, lambda r: httpx.Response(401, json={"error": "access_required"}))
    with pytest.raises(ContractCallError, match="APP_ACCESS_CODE"):
        await p.markets()


# ================================================================ the batch


async def test_trade_is_one_batch_for_one_approval_with_the_first_transaction_simulated(monkeypatch) -> None:
    trade = {
        "transactions": [{"transaction": batch_tx("redeem"), "label": "Redeem pooled shares", "legIndexes": [0], "makerSigned": True},
                         {"transaction": batch_tx("sell"), "label": "Sell YES", "legIndexes": [0], "makerSigned": True}],
        "quotes": [{"legIndex": 0, "marketId": MARKET, "outcome": "yes", "side": "sell", "amountIn": 5, "amountOut": 3.8, "price": 0.767,
                    "payoutIfWin": 0, "pooled": 0, "redeemedShares": 5}],
        "lastValidBlockHeight": 99, "mergeMarkets": [], "warnings": ["Sold below the last price"],
    }
    sim = Sim()
    p = contract(monkeypatch, app(trade), sim=sim)
    env = (await p.trade(OWNER.pubkey(), [{"market": MARKET, "outcome": "yes", "side": "sell", "amount": "5"}])).to_dict()
    assert env["simulation_success"] and sim.calls == 1 and env["programId"] == WORLD_PROGRAM_ID
    t1, t2 = env["transactions"]
    assert (t1["id"], t2["id"], t2["dependsOn"]) == ("predict-1", "predict-2", ["predict-1"])
    assert t1["simulation"]["status"] == "success" and t2["simulation"]["status"] == "not_run_dependency"
    assert t1["requiredSigners"] == [str(OWNER.pubkey()), str(MAKER.pubkey())] and t1["lastValidBlockHeight"] == "99"
    s = env["summary"]
    assert s["batch"] and s["sign_all_at_once"] and s["send_in_order"] and s["co_signer"] == str(MAKER.pubkey())
    assert s["after_confirm"] is None and s["warnings"] == ["Sold below the last price"] and s["quotes"][0]["cash_out"] == 3.8


@pytest.mark.parametrize("tx, message", [
    (lambda: batch_tx(payer=MAKER, co=OWNER), "not paid for by your wallet"),
    (lambda: batch_tx(co=Keypair()), "signer other than your wallet"),
    (lambda: batch_tx(maker_signs=False), "missing the app maker's signature"),
    (lambda: batch_tx(program=MEMO), "outside Predict"),
])
async def test_a_batch_with_anything_unexpected_is_refused(monkeypatch, tx, message) -> None:
    trade = {"transactions": [{"transaction": tx(), "label": "x", "legIndexes": [0], "makerSigned": True}], "quotes": [],
             "lastValidBlockHeight": 1, "mergeMarkets": []}
    p = contract(monkeypatch, app(trade))
    with pytest.raises(ContractCallError, match=message):
        await p.trade(OWNER.pubkey(), [{"market": MARKET, "outcome": "yes", "amount": "1"}])


async def test_merge_and_faucet(monkeypatch) -> None:
    p = contract(monkeypatch, app())
    assert (await p.merge([MARKET]))["results"][0]["merged_usdc"] == 1.27
    with pytest.raises(ValueError):
        await p.merge([])
    got = await p.faucet(OWNER.pubkey())
    assert got["added_cash"] == 100 and got["cash"] == 1100
    with pytest.raises(ValueError, match="at most 1000"):
        await p.faucet(OWNER.pubkey(), "5000")
    with pytest.raises(ContractCallError, match="test fork"):
        await contract(monkeypatch, app(), cluster="mainnet").faucet(OWNER.pubkey())


# ================================================================ the sign page: one approval, sent in order, merged


class Net:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, signed_b64: str) -> str:
        self.sent.append(signed_b64)
        return str(len(self.sent)) * 64

    async def statuses(self, sigs):
        return [{"err": None, "confirmationStatus": "confirmed"} for _ in sigs]


def cosigned(unsigned_b64: str) -> str:
    """The wallet's signature added to the maker's (what signAllTransactions returns)."""
    tx = VersionedTransaction.from_bytes(base64.b64decode(unsigned_b64))
    sig = OWNER.sign_message(to_bytes_versioned(tx.message))
    return base64.b64encode(bytes(VersionedTransaction.populate(tx.message, [sig, *list(tx.signatures)[1:]]))).decode()


async def test_the_sign_page_sends_a_batch_in_order_and_merges_after_it_confirms(monkeypatch) -> None:
    get_settings.cache_clear()
    safety.set_safety(Safety(MemoryRecordStore()))
    wallet = str(OWNER.pubkey())
    txs = [batch_tx("buy-a"), batch_tx("buy-b")]
    calls: list[tuple[str, dict]] = []

    async def call_raw(tool, **a):
        calls.append((tool, a))
        if a.get("action") == "merge":
            return {"results": [{"market_id": MARKET, "merged_usdc": 2.5}]}
        return {"transactions": [{"id": f"predict-{i + 1}", "purpose": f"leg {i + 1}", "unsignedTransaction": t, "lastValidBlockHeight": "9"}
                                 for i, t in enumerate(txs)],
                "simulation_success": True, "summary": {"batch": True, "co_signer": str(MAKER.pubkey()), "merge_markets": [MARKET]}}

    monkeypatch.setattr(internal, "call_raw", call_raw)
    net = Net()
    eng = PlanEngine(MemoryRecordStore(), safety.get_safety(), net.send, net.statuses, lambda: "http://127.0.0.1:8000")
    args = {"action": "trade", "authority": wallet, "market": MARKET, "outcome": "yes", "amount": "2"}
    view = await eng.create_from_call(wallet, "vanna_predict", args, describe_call("vanna_predict", args))
    approval = base64.b64encode(bytes(OWNER.sign_message(view["approval_message"].encode()))).decode()
    await eng.approve(view["plan_id"], wallet, approval)
    built = await eng.build_next(view["plan_id"])
    assert built["batch"] and [t["id"] for t in built["transactions"]] == ["predict-1", "predict-2"]

    with pytest.raises(PlanError) as order:
        await eng.submit(view["plan_id"], "predict-2", cosigned(txs[1]))
    assert order.value.code == "out_of_order"
    stripped = VersionedTransaction.from_bytes(base64.b64decode(cosigned(txs[0])))
    stripped = VersionedTransaction.populate(stripped.message, [stripped.signatures[0], Signature.default()])
    with pytest.raises(PlanError) as only_wallet:  # the maker's signature must still be in it and valid
        await eng.submit(view["plan_id"], "predict-1", base64.b64encode(bytes(stripped)).decode())
    assert only_wallet.value.code == "invalid_signature"

    first = await eng.submit(view["plan_id"], "predict-1", cosigned(txs[0]))
    assert first["next"] == "predict-2" and len(net.sent) == 1
    await eng.submit(view["plan_id"], "predict-2", cosigned(txs[1]))
    assert len(net.sent) == 2
    done = await eng.get(view["plan_id"])
    assert done["status"] == "completed" and done["steps"][0]["state"] == "confirmed"
    assert ("vanna_predict", {"action": "merge", "market_ids": [MARKET]}) in calls
    assert any("merged 2.5 USDC" in n for n in done["steps"][0]["notes"])
    safety.set_safety(None)


async def test_a_direct_write_with_no_plan_op_gets_a_sign_link(monkeypatch) -> None:
    """Vault, perps and Predict writes are 'raw' steps: the sign page accepts them (it refused them before)."""
    get_settings.cache_clear()
    safety.set_safety(Safety(MemoryRecordStore()))
    eng = PlanEngine(MemoryRecordStore(), safety.get_safety(), Net().send, Net().statuses, lambda: "http://127.0.0.1:8000")
    wallet = str(OWNER.pubkey())
    args = {"action": "deposit", "authority": wallet, "vault": "basis-eth", "amount": "20"}
    view = await eng.create_from_call(wallet, "vanna_vaults", args, describe_call("vanna_vaults", args))
    assert view["steps"][0]["label"] == "Deposit 20 USDC into ETH Basis" or view["steps"][0]["label"].startswith("Deposit 20 USDC into")
    with pytest.raises(PlanError, match="Unsupported direct step"):
        await eng.create_from_call(wallet, "vanna_vaults", {"action": "list"}, "List")
    safety.set_safety(None)
