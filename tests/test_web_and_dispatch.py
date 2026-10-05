"""Offline tests for the signing pages/API, the new dispatchers (vanna_plan, vanna_risk, vanna_portfolio) and the
direct-write safety layer + signing links wired into the scoped server. No RPC, no network."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from mcp import Client
from solders.keypair import Keypair
from starlette.applications import Starlette
from starlette.testclient import TestClient

import mcp_server.main as main
from mcp_server import safety
from mcp_server.dispatchers.plan import vanna_plan
from mcp_server.dispatchers.portfolio import vanna_portfolio
from mcp_server.dispatchers.risk_advisor import vanna_risk
from mcp_server.oauth.store import MemoryRecordStore
from mcp_server.planning.engine import PlanEngine, set_engine
from mcp_server.safety import Safety
from mcp_server.web.routes import build_web_routes, is_web_public_path
from tests._fakechain import FakeChain, install, sign_b64, sign_wrong_key
from tests.test_planning import Net
from vanna_core.config import get_settings

KP = Keypair()
WALLET = str(KP.pubkey())
BASE = "http://127.0.0.1:8000"


@pytest.fixture(autouse=True)
def _state():
    get_settings.cache_clear()
    safety.set_safety(Safety(MemoryRecordStore()))
    yield
    set_engine(None)
    safety.set_safety(None)
    get_settings.cache_clear()


def approval_sig(view, kp: Keypair = KP) -> str:
    return base64.b64encode(bytes(kp.sign_message(view["approval_message"].encode()))).decode()


def chain(monkeypatch, **kw) -> FakeChain:
    return install(monkeypatch, FakeChain(wallet=WALLET, **kw))


def engine_with(net: Net | None = None) -> tuple[PlanEngine, Net]:
    net = net or Net()
    eng = PlanEngine(MemoryRecordStore(), safety.get_safety(), net.send, net.statuses, lambda: BASE)
    set_engine(eng)
    return eng, net


# ================================================================ pages and API


@pytest.fixture
def web():
    return TestClient(Starlette(routes=build_web_routes()))


async def test_pages_are_static_files_under_a_strict_csp(web: TestClient) -> None:
    page = web.get("/plan/anything")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp and "default-src 'none'" in csp
    assert page.headers["cache-control"] == "no-store" and page.headers["x-frame-options"] == "DENY"
    html = page.text
    assert "<script>" not in html and "onclick" not in html  # no inline script anywhere
    assert "style=" not in html and "<style" not in html  # nor inline style: the CSP would block it
    assert 'src="/static/plan.js"' in html and "solana-web3" in html
    connect = web.get("/connect")
    assert connect.status_code == 200 and "connect.js" in connect.text
    assert web.get("/sign/abc", follow_redirects=False).headers["location"] == "/plan/abc"


def test_static_files_are_whitelisted_and_the_vendored_library_is_intact(web: TestClient) -> None:
    for name, kind in (("vanna.css", "text/css"), ("wallet.js", "javascript"), ("plan.js", "javascript"), ("connect.js", "javascript")):
        r = web.get(f"/static/{name}")
        assert r.status_code == 200 and kind in r.headers["content-type"] and r.headers["x-content-type-options"] == "nosniff"
    lib = web.get("/static/solana-web3-1.98.4.min.js")
    assert lib.status_code == 200 and lib.text.startswith("var solanaWeb3=")
    import hashlib

    assert hashlib.sha256(lib.content).hexdigest() == "09cdbea951b2ed0e11bcbe3aeb1ee9f035f9fb51ed212aca645475ae82688cc3"
    for bad in ("../routes.py", "..%2froutes.py", "secret.js", "routes.py", "plan.js/../../routes.py"):
        assert web.get(f"/static/{bad}").status_code == 404


def test_page_scripts_never_use_innerhtml_or_eval() -> None:
    """The pages render server text with textContent only; a regression here would be an XSS hole."""
    from pathlib import Path

    for name in ("plan.js", "connect.js", "wallet.js"):
        src = (Path(main.__file__).parent / "web" / "static" / name).read_text(encoding="utf8")
        for forbidden in ("innerHTML", "outerHTML", "eval(", "document.write", "insertAdjacentHTML", "new Function"):
            assert forbidden not in src, (name, forbidden)


def test_public_path_helper() -> None:
    assert all(is_web_public_path(p) for p in ("/connect", "/plan/x", "/sign/x", "/static/a.js", "/api/plan/x/next"))
    assert not any(is_web_public_path(p) for p in ("/mcp", "/", "/api/other", "/planx"))


async def test_the_full_browser_flow_over_http(monkeypatch, web: TestClient) -> None:
    c = chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 100.0}, wallet_tokens={"USDC": 300.0}, sol_balance=5.0)
    eng, net = engine_with()
    drawn = await vanna_plan(action="custom", authority=WALLET, steps=[{"op": "deposit", "asset": "USDC", "amount": "50"}])
    assert drawn["ok"] and drawn["executable"] and drawn["plan_url"].startswith(BASE + "/plan/")
    pid = drawn["plan_id"]
    api = f"/api/plan/{pid}"

    view = web.get(api).json()
    assert view["status"] == "awaiting_approval" and view["approval_message"] and "wallet" in view
    assert "transactions" not in json.dumps(view)  # the read view never carries a transaction

    wrong = web.post(f"{api}/approve", json={"wallet": str(Keypair().pubkey()), "signature": approval_sig(view)})
    assert wrong.status_code == 403 and wrong.json()["error"] == "wrong_wallet"
    assert web.post(f"{api}/next").status_code == 409  # not approved yet

    ok = web.post(f"{api}/approve", json={"wallet": WALLET, "signature": approval_sig(view)})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"

    nxt = web.post(f"{api}/next").json()
    assert nxt["ok"] and nxt["transactions"] and nxt["label"].startswith("Deposit")
    tx = nxt["transactions"][0]

    bad = web.post(f"{api}/submit", json={"tx_id": tx["id"], "signed": sign_wrong_key(tx["unsignedTransaction"], Keypair())})
    assert bad.status_code in (401, 400) and not net.sent
    unsigned = web.post(f"{api}/submit", json={"tx_id": tx["id"], "signed": tx["unsignedTransaction"]})
    assert unsigned.status_code == 401 and not net.sent

    sub = web.post(f"{api}/submit", json={"tx_id": tx["id"], "signed": sign_b64(tx["unsignedTransaction"], KP)})
    assert sub.status_code == 200 and sub.json()["signature"] and len(net.sent) == 1
    replay = web.post(f"{api}/submit", json={"tx_id": tx["id"], "signed": sign_b64(tx["unsignedTransaction"], KP)})
    assert replay.status_code in (404, 409) or replay.status_code == 409  # not_open (completed) or not_built
    assert len(net.sent) == 1  # exactly once

    final = web.get(api).json()
    assert final["status"] == "completed" and final["steps"][0]["state"] == "confirmed"

    # The model's view, scoped to its wallet.
    status = await vanna_plan(action="status", authority=WALLET, plan_id=pid)
    assert status["status"] == "completed"
    assert (await vanna_plan(action="status", authority=str(Keypair().pubkey()), plan_id=pid))["error"] == "not_found"


def test_the_api_rejects_bad_input_and_unknown_plans(web: TestClient) -> None:
    engine_with()
    assert web.get("/api/plan/short").status_code == 404
    assert web.get("/api/plan/" + "a" * 43).status_code == 404
    assert web.post("/api/plan/" + "a" * 43 + "/approve", content=b"not json").status_code in (400, 404)
    big = web.post("/api/plan/" + "a" * 43 + "/submit", content=b"x" * 20_000)
    assert big.status_code in (413, 400, 404)
    assert web.post("/api/plan/" + "a" * 43 + "/cancel").status_code == 404


def test_endpoints_say_so_when_signing_is_not_enabled(web: TestClient) -> None:
    set_engine(None)
    r = web.get("/api/plan/" + "a" * 43)
    assert r.status_code == 503 and r.json()["error"] == "not_available"


# ================================================================ vanna_plan dispatcher


async def test_plan_tool_draws_lever_with_a_link_and_refuses_what_it_should(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10})
    eng, _ = engine_with()
    out = await vanna_plan(action="lever", authority=WALLET, asset="SOL", leverage=2, borrow_asset="USDC")
    assert out["ok"] and out["executable"] and out["status"] == "awaiting_approval" and out["plan_url"]
    assert out["simulation"]["hf_after"] >= 1.15 and "approval_message" in out
    ask = await vanna_plan(action="lever", authority=WALLET, asset="SOL", leverage=2)
    assert ask["ok"] is False and ask["options"]
    assert (await vanna_plan(action="lever", authority=WALLET, asset="SOL", leverage=9, borrow_asset="USDC"))["ok"] is False
    assert (await vanna_plan(action="lever", authority=WALLET, leverage=2))["error"] == "invalid_input"
    assert (await vanna_plan(action="unwind"))["error"] == "invalid_input"  # no wallet
    assert (await vanna_plan(action="status", authority=WALLET))["error"] == "invalid_input"


async def test_plan_tool_without_a_signing_page_previews_but_cannot_run(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10})
    out = await vanna_plan(action="lever", authority=WALLET, asset="SOL", leverage=2, borrow_asset="USDC")
    assert out["ok"] and out["executable"] is False and "ENABLE_SIGN_LINKS" in out["note"] and "plan_url" not in out
    assert (await vanna_plan(action="status", authority=WALLET, plan_id="x" * 43))["error"] == "not_available"


async def test_custom_plans_validate_resolve_percentages_and_check_funding(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 200.0}, wallet_tokens={"USDC": 80.0}, sol_balance=5.0)
    engine_with()
    ok = await vanna_plan(action="custom", authority=WALLET, steps=[
        {"op": "deposit", "asset": "USDC", "amount": "50%"}, {"op": "repay", "asset": "USDC", "amount": "all", "source": "wallet"}, {"op": "report"}])
    assert ok["ok"] and ok["steps"][0]["amount"] == "40"  # 50% of the live 80 USDC, resolved once
    for steps, text in (
        ([{"op": "deposit", "asset": "USDC", "amount": "-5"}], "positive"),
        ([{"op": "deposit", "asset": "USDC", "amount": "150%"}], "between 0 and 100"),
        ([{"op": "deposit", "asset": "BONK", "amount": "5"}], "not part of the product"),
        ([{"op": "borrow", "asset": "JitoSOL", "amount": "5"}], "lending pool"),
        ([{"op": "deposit", "asset": "USDC", "amount": "9999"}], "needs 9999"),
        ([{"op": "fly", "asset": "SOL", "amount": "1"}], "Unsupported"),
        ([{"asset": "SOL"}], "needs an 'op'"),
        ([], None),
    ):
        r = await vanna_plan(action="custom", authority=WALLET, steps=steps)
        assert (r.get("ok") is False and (text is None or text in r["message"])) or (text is None and r.get("error"))


async def test_exact_out_swap_plan(monkeypatch) -> None:
    chain(monkeypatch, sol_balance=5.0, wallet_tokens={})
    engine_with()
    out = await vanna_plan(action="swap_exact_out", authority=WALLET, from_asset="SOL", to_asset="USDC", receive_amount="100")
    assert out["ok"] and out["steps"][0]["amount"] == "100" or out["steps"][0]["label"].startswith("Swap")
    assert "at least 100 USDC" in out["steps"][0]["label"]
    assert (await vanna_plan(action="swap_exact_out", authority=WALLET, from_asset="SOL", to_asset="USDC", receive_amount="-1"))["error"] == "invalid_input"


# ================================================================ vanna_risk / vanna_portfolio


async def test_risk_policy_states_the_rules_and_the_refusals(monkeypatch) -> None:
    p = await vanna_risk(action="policy")
    assert p["health_factor_floor"] == 1.15 and p["liquidation_health_factor"] == 1.10 and p["max_plan_steps"] == 8
    assert p["assets"]["lend_borrow"] == ["SOL", "USDC", "USDT"] and p["max_action_usd"] == 5000
    text = " ".join(p["refused"]).lower()
    for needle in ("liquidat", "admin", "arbitrary", "multi-asset", "perpetual", "conditional", "other than the connected"):
        assert needle in text


async def test_risk_capacity_what_if_and_distance(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 400.0})
    cap = await vanna_risk(action="capacity", authority=WALLET, what="borrow", asset="USDC")
    row = cap["capacity"][0]
    assert row["asset"] == "USDC" and float(row["safe_amount"]) > 0 and row["health_factor_after"] >= 1.15
    assert (await vanna_risk(action="capacity", authority=WALLET, what="borrow", asset="JitoSOL"))["error"] == "asset_not_offered"
    wd = await vanna_risk(action="capacity", authority=WALLET, what="withdraw", asset="SOL")
    assert float(wd["capacity"][0]["safe_amount"]) * 100 <= 1000 - 1.15 * 400 + 1e-6

    yes = await vanna_risk(action="what_if", authority=WALLET, op="borrow", asset="USDC", amount="100")
    assert yes["allowed"] and yes["within_floor"] and "Yes" in yes["headline"]
    no = await vanna_risk(action="what_if", authority=WALLET, op="borrow", asset="USDC", amount="9000")
    assert not no["within_floor"] and no["health_factor_after"] < 1.15
    repay = await vanna_risk(action="what_if", authority=WALLET, op="repay", asset="USDC", amount="100")
    assert repay["health_factor_after"] == pytest.approx(1000 / 300)
    target = await vanna_risk(action="what_if", authority=WALLET, op="repay", asset="USDC", target_hf=4.0)
    assert float(target["repay_amount"]) == pytest.approx(400 - 1000 / 4, abs=0.01)
    assert (await vanna_risk(action="what_if", authority=WALLET, op="borrow", asset="USDC", target_hf=1.05))["error"] == "invalid_input"
    assert (await vanna_risk(action="what_if", authority=WALLET, op="borrow", asset="USDC"))["error"] == "invalid_input"

    dist = await vanna_risk(action="liquidation_distance", authority=WALLET)
    assert dist["collateral_drop_to_liquidation_pct"] == pytest.approx((1 - 1.10 / 2.5) * 100, abs=0.01) and not dist["is_liquidatable"]


async def test_risk_max_leverage_table(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10})
    out = await vanna_risk(action="max_leverage", authority=WALLET, asset="SOL")
    assert out["max_leverage"] > 1 and {r["leverage"]: r["allowed"] for r in out["table"]}[1.5] is True
    assert {r["leverage"]: r["allowed"] for r in out["table"]}[10] is False
    assert (await vanna_risk(action="max_leverage", authority=WALLET, asset="JitoSOL"))["error"] == "invalid_input"


async def test_risk_reads_fail_closed_without_an_account(monkeypatch) -> None:
    chain(monkeypatch, has_account=False)
    assert (await vanna_risk(action="capacity", authority=WALLET))["error"] == "account_not_found"


async def test_risk_funding_check(monkeypatch) -> None:
    chain(monkeypatch, wallet_tokens={"USDC": 10.0}, sol_balance=5.0)
    short = await vanna_risk(action="funding_check", authority=WALLET, steps=[{"kind": "write", "op": "deposit", "asset": "USDC", "amount": "50", "slots": {}}])
    assert short["funded"] is False and "needs 50" in short["message"]


async def test_portfolio_overview_counts_kamino_receipts_once(monkeypatch) -> None:
    chain(monkeypatch, collateral={"SOL": 10, "USDC": 200.0}, debt={"USDC": 300.0}, wallet_tokens={"USDC": 50.0}, sol_balance=2.0,
          earn_shares={"USDC": 40.0}, farm={"SOL": 3.0})
    out = await vanna_portfolio(action="overview", authority=WALLET)
    m = out["margin"]
    assert m["collateral_usd"] == 1200 and m["debt_usd"] == 300 and m["equity_usd"] == 900
    assert m["leverage"] == pytest.approx(1200 / 900, abs=0.01) and m["ltv_pct"] == 25.0
    assert out["wallet"]["usd"] == pytest.approx(2 * 100 + 50) and out["earn"][0]["pool"] == "USDC" and out["farm"][0]["receipt"] == "kSOL"
    assert out["net_worth_usd"] == pytest.approx(250 + 900 + 40)  # wallet + margin equity + earn; farm is inside margin
    assert "unavailable" not in out


async def test_portfolio_names_what_it_could_not_read(monkeypatch) -> None:
    c = chain(monkeypatch)
    c.has_account = False
    out = await vanna_portfolio(action="overview", authority=WALLET)
    assert out["margin"]["account"] is None and "No margin account" in out["margin"]["note"]
    assert (await vanna_portfolio(action="nope", authority=WALLET))["error"] == "invalid_input"


# ================================================================ scoped server: safety layer + signing links


def _stub_server():
    server = main.ScopedMCPServer(name="guard-test")
    fake = {"c": None}

    async def vanna_margin_trade(action: str, authority: str, asset: str = "USDC", amount: str = "1", repay_all: bool = False,
                                 payer: str | None = None) -> dict[str, Any]:
        return fake["c"]._envelope(authority, f"margin:{action}")

    async def vanna_earn_write(action: str, owner: str, asset: str = "USDC", amount: str | None = None, shares: str | None = None) -> dict[str, Any]:
        return fake["c"]._envelope(owner, f"earn:{action}")

    server.add_tool(vanna_margin_trade)
    server.add_tool(vanna_earn_write)
    return server, fake


async def call_tool(server, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    async with Client(server) as client:
        result = await client.call_tool(name, args)
    block = result.content[0]
    return json.loads(block.text), result.is_error


async def test_direct_writes_get_the_floor_the_limits_and_a_signing_link(monkeypatch) -> None:
    monkeypatch.setenv("SAFETY_LAYER", "true")
    get_settings.cache_clear()
    c = chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 600.0}, wallet_tokens={"USDC": 1000.0})
    eng, _ = engine_with()
    server, fake = _stub_server()
    fake["c"] = c

    refused, is_error = await call_tool(server, "vanna_margin_trade", {"action": "borrow", "authority": WALLET, "asset": "USDC", "amount": "2700"})
    assert is_error and refused["error"] == "health_check_failed" and "1.15" in refused["message"]
    ok, is_error = await call_tool(server, "vanna_margin_trade", {"action": "borrow", "authority": WALLET, "asset": "USDC", "amount": "100"})
    assert not is_error and ok["simulation_success"] is True
    assert ok["sign_url"].startswith(BASE + "/plan/") and ok["signing"]["available"] and ok["plan_id"]
    view = await eng.get(ok["plan_id"])
    assert view["kind"] == "single" and view["wallet"] == WALLET and view["steps"][0]["label"].startswith("Borrow")

    junk, is_error = await call_tool(server, "vanna_margin_trade", {"action": "deposit", "authority": WALLET, "asset": "USDC", "amount": "1.1234567"})
    assert is_error and junk["error"] == "invalid_input" and "decimal" in junk["message"]
    low, is_error = await call_tool(server, "vanna_margin_trade", {"action": "deposit", "authority": WALLET, "asset": "USDC", "amount": "-4"})
    assert is_error and "positive" in low["message"]


async def test_a_failed_simulation_gets_no_signing_link(monkeypatch) -> None:
    monkeypatch.setenv("SAFETY_LAYER", "true")
    get_settings.cache_clear()
    c = chain(monkeypatch, collateral={"SOL": 10}, wallet_tokens={"USDC": 1000.0})
    engine_with()
    server, fake = _stub_server()
    fake["c"], c.simulation_ok = c, False
    out, is_error = await call_tool(server, "vanna_margin_trade", {"action": "deposit", "authority": WALLET, "asset": "USDC", "amount": "5"})
    assert "sign_url" not in out and out["signing"]["available"] is False


async def test_the_safety_layer_is_off_by_default_so_other_deployments_are_unchanged(monkeypatch) -> None:
    c = chain(monkeypatch, collateral={"SOL": 10}, debt={"USDC": 600.0})
    server, fake = _stub_server()
    fake["c"] = c
    out, is_error = await call_tool(server, "vanna_margin_trade", {"action": "borrow", "authority": WALLET, "asset": "USDC", "amount": "2700"})
    assert not is_error and "sign_url" not in out  # no engine, no guard: exactly the previous behaviour


async def test_write_rate_limit_and_daily_spend_are_per_wallet(monkeypatch) -> None:
    monkeypatch.setenv("WRITE_RATE_PER_MINUTE", "2")
    get_settings.cache_clear()
    chain(monkeypatch, collateral={"SOL": 10})
    eng, _ = engine_with()
    pid = None
    for i in range(3):
        view = await eng.create(WALLET, {"summary": "x", "steps": [{"kind": "write", "op": "deposit", "asset": "USDC", "amount": "1", "slots": {}}],
                                         "warnings": [], "simulation": None, "hf_floor": 1.15}, kind="custom")
        await eng.approve(view["plan_id"], WALLET, approval_sig(view))
        r = await eng.build_next(view["plan_id"])
        assert (r.get("ok") is True) == (i < 2), r
    other = str(Keypair().pubkey())
    assert (await safety.get_safety().consume_write(other)).ok  # another wallet has its own budget


async def test_faucet_is_capped_per_wallet_per_day(monkeypatch) -> None:
    monkeypatch.setenv("FAUCET_REQUESTS_PER_WALLET_PER_DAY", "2")
    get_settings.cache_clear()
    s = safety.get_safety()
    assert (await s.consume_faucet(WALLET)).ok and (await s.consume_faucet(WALLET)).ok
    third = await s.consume_faucet(WALLET)
    assert not third.ok and third.to_error()["error"] == "limit_exceeded" and third.retry_after_s > 0
    assert (await s.consume_faucet(str(Keypair().pubkey()))).ok
