"""Live end-to-end test of the hosted transport, exactly as a Copilot client uses it.

Starts the real server process (`python -m mcp_server.main`, streamable-http, no auth), then speaks raw
HTTP to it the way `mercury-stellar-backend/lib/copilot/mcp-client.ts` does: POST `initialize`, read the
`mcp-session-id` header, send `notifications/initialized`, then `tools/list` and `tools/call`, parsing
SSE `message` frames. Every answer is cross-checked against a direct read of the same on-chain state, so
the transport can't silently change or invent values. Also pins the stale-session behaviour the client's
retry depends on (unknown session -> 404).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from solders.keypair import Keypair

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.asyncio


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    external = os.environ.get("MCP_E2E_URL")  # e.g. a running container: http://127.0.0.1:18080
    if external:
        yield external.rstrip("/")
        return
    port = _free_port()
    env = {**os.environ, "MCP_TRANSPORT": "streamable-http", "PORT": str(port), "AUTH_ENABLED": "false",
           "MCP_HTTP_HOST": "127.0.0.1", "LOG_LEVEL": "WARNING"}
    proc = subprocess.Popen([sys.executable, "-m", "mcp_server.main"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early:\n{proc.stdout.read().decode(errors='replace')}")
        try:
            if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("server did not become healthy within 60 s")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _parse(resp: httpx.Response) -> dict:
    """A JSON-RPC response from either a JSON body or SSE `data:` frames (the last one wins)."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        messages = [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]
        return messages[-1]
    return resp.json()


class Client:
    def __init__(self, base: str) -> None:
        self.url = f"{base}/mcp"
        self.http = httpx.AsyncClient(timeout=180)
        self.session: str | None = None
        self._id = 0
        self.rate_limited = 0

    async def rpc(self, method: str, params: dict | None = None, session: str | None = "own") -> httpx.Response:
        self._id += 1
        headers = dict(HEADERS)
        sid = self.session if session == "own" else session
        if sid:
            headers["mcp-session-id"] = sid
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        # The rate limiter stays on (it is part of the deployed pipeline). Behave like a well-behaved client:
        # on 429, check the contract (structured body + Retry-After) and retry after the advertised wait.
        for _ in range(4):
            resp = await self.http.post(self.url, headers=headers, json=body)
            if resp.status_code != 429:
                return resp
            assert resp.json()["error"] == "rate_limited", resp.text
            self.rate_limited += 1
            await asyncio.sleep(int(resp.headers["retry-after"]))
        return resp

    async def initialize(self) -> dict:
        resp = await self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "e2e-test", "version": "1"},
        }, session=None)
        assert resp.status_code == 200, resp.text
        self.session = resp.headers.get("mcp-session-id")
        assert self.session, "initialize must return mcp-session-id (the Copilot client requires it)"
        result = _parse(resp)
        await self.http.post(self.url, headers={**HEADERS, "mcp-session-id": self.session},
                             json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    async def call(self, tool: str, **arguments) -> dict:
        resp = await self.rpc("tools/call", {"name": tool, "arguments": arguments})
        assert resp.status_code == 200, resp.text
        message = _parse(resp)
        assert "error" not in message, message
        result = message["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            # Rejections before a dispatcher runs (arguments, scope, circuit): still our error JSON, and short
            # enough for the Copilot client, which parses only the first 300 characters.
            assert len(text) <= 300, text
            payload = json.loads(text)
            assert isinstance(payload.get("error"), str), payload
            return payload
        return json.loads(text)

    async def close(self) -> None:
        await self.http.aclose()


@pytest.fixture
async def client(server_url):
    c = Client(server_url)
    await c.initialize()
    yield c
    await c.close()


async def test_health_and_tool_list(server_url, client):
    health = httpx.get(f"{server_url}/healthz", timeout=10).json()
    assert health.get("status") in ("ok", "healthy") or health.get("ok") is True, health
    resp = await client.rpc("tools/list")
    tools = {t["name"] for t in _parse(resp)["result"]["tools"]}
    expected = {
        "vanna_protocol_info", "vanna_oracle", "vanna_earn_market", "vanna_earn_position", "vanna_earn_write",
        "vanna_account", "vanna_margin_status", "vanna_margin_trade", "vanna_wallet", "vanna_history",
        "vanna_farm_overview", "vanna_farm_kamino", "vanna_swap", "vanna_sign",
    }
    assert tools == expected  # 14 dispatchers; the dev-only vanna_fork is off by default


async def test_unknown_session_is_404_so_the_client_rehandshakes(client):
    resp = await client.rpc("tools/list", session="not-a-real-session-id")
    assert resp.status_code == 404, (resp.status_code, resp.text)


async def test_reads_match_direct_on_chain_state(client, ctx):
    """Values over HTTP equal a direct read of the same accounts (no transport-side invention)."""
    from vanna_core.assets import AssetRegistry

    assets = await client.call("vanna_protocol_info", action="assets")
    registry = AssetRegistry(ctx.client)
    await registry.load()
    direct = {a.symbol: a for a in registry.all()}
    over_http = {a["symbol"]: a for a in assets["assets"]}
    assert set(over_http) == set(direct)
    for symbol, row in over_http.items():
        d = direct[symbol]
        assert (row["mint"], row["asset_index"], row["decimals"], row["borrow_enabled"]) == \
               (d.mint, d.asset_index, d.decimals, d.borrow_enabled), symbol

    # Pool config fields are static (only an admin changes them): they must match the Reserve exactly.
    pool = await client.call("vanna_earn_market", action="pool_stats", asset="USDC")
    reserve = await ctx.reserve("USDC").get_reserve()
    assert pool["asset_symbol"] == "USDC"
    assert pool["reserve_address"] == str(ctx.reserve("USDC").reserve_address)
    assert (pool["reserve_factor_bps"], pool["status"]) == (reserve.reserve_factor_bps, reserve.status)
    assert (pool["linear_coeff_wad"], pool["jump_coeff_wad"], pool["rate_multiplier_wad"]) == (
        str(reserve.rate_curve.linear_coeff_wad), str(reserve.rate_curve.jump_coeff_wad),
        str(reserve.rate_curve.rate_multiplier_wad),
    )

    # A price moves, but it must be the program's own reading of the same oracle accounts: compare to a
    # direct read taken right after, within a tolerance for the seconds in between.
    from decimal import Decimal

    price = await client.call("vanna_oracle", action="price", asset="SOL")
    direct_price = await ctx.oracle.get_price("SOL")
    assert price["source"] == direct_price["source"]
    over, direct_value = Decimal(price["price_usd"]), Decimal(direct_price["price_usd"])
    assert abs(over - direct_value) / direct_value < Decimal("0.02"), (over, direct_value)


async def test_errors_are_structured_not_crashes(client):
    fresh = str(Keypair().pubkey())
    assert (await client.call("vanna_account", action="resolve", authority=fresh))["exists"] is False
    assert (await client.call("vanna_oracle", action="price", asset="DOGE"))["error"] == "invalid_input"
    assert (await client.call("vanna_earn_market", action="pool_stats", asset="JitoSOL"))["error"] == "asset_not_offered"
    assert (await client.call("vanna_margin_trade", action="borrow", authority=fresh, asset="JupSOL", amount="1"))["error"] == "asset_not_offered"
    # Rejected by the SDK's argument validation before a dispatcher runs: same error contract.
    bad_action = await client.call("vanna_sign", action="sign_and_submit")
    assert bad_action["error"] == "invalid_input" and "tx_status" in bad_action["message"]
    missing = await client.call("vanna_account", action="get")
    assert missing["error"] == "invalid_input" and "authority" in missing["message"]
    unknown = await client.call("vanna_does_not_exist", action="x")
    assert unknown["error"] == "invalid_input"


async def test_write_returns_simulated_unsigned_envelope(client, funded_fork_pubkey):
    envelope = await client.call("vanna_account", action="create", authority=funded_fork_pubkey)
    assert envelope["schemaVersion"] == "1"
    assert envelope["simulation_success"] is True, envelope.get("reason")
    assert envelope["signing_status"] == "needs_wallet_sign"
    tx = envelope["transactions"][0]
    assert tx["unsignedTransaction"] and tx["requiredSigners"] == [funded_fork_pubkey]
