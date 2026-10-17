"""Vanna Solana MCP dispatcher tools.

Exports ALL_DISPATCHERS — the flat list of dispatcher tool functions
registered with the MCP server in main.py. Each dispatcher takes an `action`
parameter selecting one of its actions (TOOLS_CATALOGUE.md §3) — this is the
14-dispatcher-tool shape, not one flat tool per instruction/read (see
TOOLS_CATALOGUE.md §3's "Shape decision").

All 14 dispatchers are implemented; `vanna_fork` (FORK_DISPATCHERS) is dev-only and
registered only with ENABLE_FORK_TOOLS=true.
"""

from __future__ import annotations

from mcp_server.dispatchers.account import vanna_account
from mcp_server.dispatchers.earn_market import vanna_earn_market
from mcp_server.dispatchers.earn_position import vanna_earn_position
from mcp_server.dispatchers.earn_write import vanna_earn_write
from mcp_server.dispatchers.farm_kamino import vanna_farm_kamino
from mcp_server.dispatchers.farm_overview import vanna_farm_overview
from mcp_server.dispatchers.history import vanna_history
from mcp_server.dispatchers.margin_status import vanna_margin_status
from mcp_server.dispatchers.margin_trade import vanna_margin_trade
from mcp_server.dispatchers.fork import vanna_fork
from mcp_server.dispatchers.oracle import vanna_oracle
from mcp_server.dispatchers.perps import vanna_perps
from mcp_server.dispatchers.plan import vanna_plan
from mcp_server.dispatchers.portfolio import vanna_portfolio
from mcp_server.dispatchers.predict import vanna_predict
from mcp_server.dispatchers.risk_advisor import vanna_risk
from mcp_server.dispatchers.protocol_info import vanna_protocol_info
from mcp_server.dispatchers.sign import vanna_sign
from mcp_server.dispatchers.swap import vanna_swap
from mcp_server.dispatchers.vaults import vanna_vaults
from mcp_server.dispatchers.wallet import vanna_wallet

ALL_DISPATCHERS = [
    vanna_protocol_info,
    vanna_oracle,
    vanna_account,
    vanna_margin_status,
    vanna_margin_trade,
    vanna_earn_market,
    vanna_earn_position,
    vanna_earn_write,
    vanna_wallet,
    vanna_history,
    vanna_farm_overview,
    vanna_farm_kamino,
    vanna_swap,
    vanna_sign,
    vanna_risk,
    vanna_portfolio,
    vanna_plan,
    vanna_perps,
    vanna_vaults,
    vanna_predict,
]

# Each tool's human title and whether it only reads. Sent to clients as MCP tool annotations (`title`,
# `readOnlyHint`, ...) so a client can run reads without asking and present writes for approval. "Writes" here only
# ever BUILD unsigned transactions and plan links: nothing moves until the user's own wallet signs.
TOOL_INFO: dict[str, tuple[str, bool]] = {
    "vanna_protocol_info": ("Protocol facts and explanations", True),
    "vanna_oracle": ("Prices", True),
    "vanna_account": ("Margin account: status, health, open / close", False),
    "vanna_margin_status": ("Margin account details and borrow / withdraw checks", True),
    "vanna_margin_trade": ("Single margin action: deposit, withdraw, borrow, repay", False),
    "vanna_earn_market": ("Lending pool rates and liquidity", True),
    "vanna_earn_position": ("Your lending (Earn) positions", True),
    "vanna_earn_write": ("Lend to or withdraw from a Vanna pool", False),
    "vanna_wallet": ("Wallet balances (and test faucet)", False),
    "vanna_history": ("Transaction history", True),
    "vanna_farm_overview": ("Kamino farm markets and positions", True),
    "vanna_farm_kamino": ("Kamino farm deposit / redeem", False),
    "vanna_swap": ("Swap quotes and swaps (Jupiter / Titan)", False),
    "vanna_sign": ("Transaction status", True),
    "vanna_risk": ("Risk advisor: safe amounts, what-if, max leverage", True),
    "vanna_portfolio": ("Portfolio overview", True),
    "vanna_plan": ("Plan and run: leverage, multi-step actions, sized amounts", False),
    "vanna_perps": ("Phoenix perpetuals", False),
    "vanna_vaults": ("Basis and Perena vaults", False),
    "vanna_predict": ("Prediction markets (World)", False),
}

# Dev-only — registered by main.py only when ENABLE_FORK_TOOLS=true. Never
# part of the Copilot's allowlist even then (TOOLS_CATALOGUE.md §3.15).
FORK_DISPATCHERS = [
    vanna_fork,
]
