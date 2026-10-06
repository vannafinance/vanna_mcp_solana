"""Offline tests for the firewall text, MCP prompts, the faucet gate, the rate curve and the concepts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from solders.keypair import Keypair

import mcp_server.main as main
from mcp_server import safety
from mcp_server.agent_firewall import AGENT_INSTRUCTIONS, get_agent_instructions
from mcp_server.dependencies import _set_context
from mcp_server.dispatchers import ALL_DISPATCHERS
from mcp_server.dispatchers.earn_market import _rate_curve
from mcp_server.dispatchers.protocol_info import CONCEPTS, vanna_protocol_info
from mcp_server.dispatchers.wallet import vanna_wallet
from mcp_server.oauth.store import MemoryRecordStore
from mcp_server.safety import Safety
from vanna_core import math_utils
from vanna_core.config import get_settings
from vanna_core.contracts.fork import ForkContract
from vanna_core.config import VannaSettings

WALLET = str(Keypair().pubkey())


@pytest.fixture(autouse=True)
def _state():
    get_settings.cache_clear()
    safety.set_safety(Safety(MemoryRecordStore()))
    yield
    _set_context(None)
    safety.set_safety(None)
    get_settings.cache_clear()


# ---------------------------------------------------------------- instructions


def test_instructions_carry_the_copilots_refusals_and_numbers() -> None:
    text = get_agent_instructions().lower()
    assert "{product_assets}" not in text
    for needle in (
        "liquidation", "admin", "arbitrary program", "multi-asset", "perpetuals", "outside the product", "other wallet",
        "conditional, scheduled or standing", "stop-loss", "never invent numbers", "never claim a transaction happened",
        "1.10", "1.15", "can be lent (earn) and borrowed", "jitosol", "jupsol", "nvdax", "jupusd", "8", "0.05 sol", "never ask for a private key",
        "decline coding help", "never reveal",
    ):
        assert needle in text, needle


def test_every_tool_is_routed_in_the_instructions() -> None:
    """A new dispatcher with no guidance would be a tool the model has to guess about."""
    for fn in ALL_DISPATCHERS:
        assert fn.__name__ in AGENT_INSTRUCTIONS, fn.__name__


def test_public_instructions_add_the_connected_wallet_rules() -> None:
    public = get_agent_instructions(public=True)
    assert public.startswith(get_agent_instructions(public=False)) and "wallet_mismatch" in public and "faucet" in public
    assert "wallet_mismatch" not in get_agent_instructions(public=False)


# ---------------------------------------------------------------- prompts


async def test_prompts_are_registered_and_render() -> None:
    names = {p.name for p in await main.mcp.list_prompts()}
    assert {"vanna_assistant", "health_check", "lever", "unwind", "earn_overview"} <= names
    lever = await main.mcp.get_prompt("lever", {"asset": "JitoSOL", "leverage": "3", "amount": "2"})
    body = lever.messages[0].content.text
    assert "JitoSOL 3x" in body and "vanna_plan lever" in body and "approve with my wallet" in body
    rules = await main.mcp.get_prompt("vanna_assistant", {})
    assert "Domain firewall" in rules.messages[0].content.text


# ---------------------------------------------------------------- faucet


class _Fork:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def faucet(self, address: str, symbol: str | None = None) -> dict:
        self.calls.append((address, symbol))
        return {"ok": True, "results": [{"symbol": symbol or "all", "recipient": address}]}


def _ctx(**settings) -> SimpleNamespace:
    ctx = SimpleNamespace(settings=VannaSettings(**settings), fork=_Fork())
    _set_context(ctx)  # type: ignore[arg-type]
    return ctx


async def test_the_faucet_is_off_unless_enabled_and_is_capped_per_wallet() -> None:
    ctx = _ctx()
    off = await vanna_wallet(action="faucet", authority=WALLET)
    assert off["error"] == "invalid_input" and "ENABLE_TEST_FAUCET" in off["message"] and not ctx.fork.calls

    ctx = _ctx(enable_test_faucet=True)
    for _ in range(3):
        assert (await vanna_wallet(action="faucet", authority=WALLET, asset="USDC"))["ok"]
    capped = await vanna_wallet(action="faucet", authority=WALLET)
    assert capped["error"] == "limit_exceeded" and capped["kind"] == "faucet" and len(ctx.fork.calls) == 3
    assert (await vanna_wallet(action="faucet", authority=str(Keypair().pubkey())))["ok"]  # another wallet, own budget
    assert (await vanna_wallet(action="faucet", authority="not-a-wallet"))["error"] in ("invalid_address", "invalid_input")


async def test_the_fork_contract_refuses_a_non_fork_cluster_and_a_disabled_faucet() -> None:
    from vanna_core.types import ContractCallError

    with pytest.raises(ContractCallError, match="disabled"):
        await ForkContract(VannaSettings()).faucet(WALLET)
    with pytest.raises(ContractCallError, match="test fork"):
        await ForkContract(VannaSettings(enable_test_faucet=True, solana_cluster_label="mainnet-beta")).faucet(WALLET)


# ---------------------------------------------------------------- rate curve + concepts


def test_the_rate_curve_matches_the_programs_math() -> None:
    wad = math_utils.WAD
    stats = {
        "linear_coeff_wad": str(wad // 10), "jump_coeff_wad": str(wad), "rate_multiplier_wad": str(wad),
        "reserve_factor_bps": 1000, "utilization_rate_pct": "40.00", "borrow_apr_pct": "5.1", "supply_apr_pct": "1.8",
    }
    out = _rate_curve("USDC", stats)
    pts = out["points"]
    assert [p["utilization_pct"] for p in pts] == list(range(0, 101, 5))
    borrow = [float(p["borrow_apr_pct"]) for p in pts]
    assert borrow == sorted(borrow) and borrow[0] == 0.0 and borrow[-1] > borrow[len(borrow) // 2]  # rises, steeply near full
    mid = pts[8]  # 40%
    assert float(mid["supply_apr_pct"]) == pytest.approx(float(mid["borrow_apr_pct"]) * 0.4 * 0.9, abs=0.001)
    assert out["current_utilization_pct"] == "40.00"


async def test_concepts_return_the_copilots_explanations() -> None:
    allc = await vanna_protocol_info(action="concepts")
    assert set(allc["concepts"]) == set(CONCEPTS) and "health_factor" in allc["concepts"]
    one = await vanna_protocol_info(action="concepts", topic="liquidation")
    assert "1.10" in one["explanation"] and "never execute a liquidation" in one["explanation"]
    assert (await vanna_protocol_info(action="concepts", topic="bogus"))["error"] == "invalid_input"
