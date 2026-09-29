"""Read/write classification of every dispatcher action.

The single table consulted by write-scope gating (mcp_server/main.py) and by
rate-limit costing (mcp_server/rate_limit.py). A "write" action builds an
unsigned transaction or moves funds on the fork — anything a read-only caller
must not be able to trigger. Reads are every
action not listed here.

tests/test_hosting.py checks each entry against the dispatcher's own
`action` Literal, so a renamed or added action cannot silently fall out of
the gate.
"""

from __future__ import annotations

WRITE_ACTIONS: dict[str, frozenset[str]] = {
    "vanna_account": frozenset({"create", "close"}),
    "vanna_margin_trade": frozenset({
        "deposit", "withdraw", "close_collateral", "open_debt", "borrow", "close_debt",
        "repay", "repay_from_wallet", "deposit_and_borrow", "close_position",
    }),
    "vanna_earn_write": frozenset({"supply", "redeem"}),
    "vanna_farm_kamino": frozenset({"deposit", "redeem"}),
    "vanna_swap": frozenset({"margin_swap", "wallet_swap"}),
    "vanna_fork": frozenset({"faucet"}),
}

# Tools whose every action writes — hidden from a read-only caller's tools/list.
# Mixed tools (vanna_account, vanna_swap) stay listed so their read
# actions remain usable; their write actions are refused per call instead.
WRITE_ONLY_TOOLS: frozenset[str] = frozenset({
    "vanna_margin_trade",
    "vanna_earn_write",
    "vanna_farm_kamino",
    "vanna_fork",
})

# Reads that fan out to many accounts / RPC calls (rate-limit cost 5, not 1).
EXPENSIVE_READ_TOOLS: frozenset[str] = frozenset({
    "vanna_farm_overview",
    "vanna_history",
    "vanna_margin_status",
    "vanna_earn_position",
    "vanna_wallet",
})


def is_write_call(tool: str, action: str | None) -> bool:
    """True when this tools/call would perform a write.

    A write-only tool counts as a write even with a missing or unknown action,
    so a malformed call cannot slip past the gate.
    """
    if tool in WRITE_ONLY_TOOLS:
        return True
    return action is not None and action in WRITE_ACTIONS.get(tool, frozenset())
