"""Wallet scoping for public-connector callers.

A caller authenticated by the connector's wallet login (identity.caller_wallet()) may only *act* as that
wallet. Reads accept any public address (chain data is public), but every write must be built for the
token's own wallet, so a prompt-injected or malicious model cannot make the server build a transaction
that spends, or even names as signer, somebody else's wallet.

This runs in `ScopedMCPServer` before the dispatcher, from one table, so no dispatcher can forget it.
`tests/test_wallet_scope.py` checks that every write action in `scopes.WRITE_ACTIONS` has an entry here
and that each named parameter exists on the dispatcher.

Callers that are not wallet-scoped (the Copilot's machine token, stdio, tests) are untouched.
"""

from __future__ import annotations

from typing import Any

# (tool, action) -> the argument that must be the caller's wallet. A tool entry with action "*" applies to
# every action of that tool.
SIGNER_PARAM: dict[tuple[str, str], str] = {
    ("vanna_account", "create"): "authority",
    ("vanna_account", "close"): "authority",
    ("vanna_earn_write", "supply"): "owner",
    ("vanna_earn_write", "redeem"): "owner",
    ("vanna_farm_kamino", "deposit"): "authority",
    ("vanna_farm_kamino", "redeem"): "authority",
    ("vanna_wallet", "faucet"): "authority",
    ("vanna_plan", "lever"): "authority",
    ("vanna_plan", "unwind"): "authority",
    ("vanna_plan", "swap_exact_out"): "authority",
    ("vanna_plan", "custom"): "authority",
    ("vanna_plan", "cancel"): "authority",
    ("vanna_swap", "margin_swap"): "authority",
    ("vanna_swap", "wallet_swap"): "authority",
    # Anyone may repay a margin account's debt from their own wallet, so the *payer* is the signer and
    # `authority` (whose account is repaid) is free.
    ("vanna_margin_trade", "repay_from_wallet"): "payer",
    ("vanna_margin_trade", "*"): "authority",
}

# Read arguments defaulted to the caller's wallet when omitted ("what's my health?").
DEFAULT_PARAM: dict[str, str] = {
    "vanna_account": "authority",
    "vanna_margin_status": "authority",
    "vanna_margin_trade": "authority",
    "vanna_earn_position": "owner",
    "vanna_earn_write": "owner",
    "vanna_farm_kamino": "authority",
    "vanna_farm_overview": "authority",
    "vanna_history": "authority",
    "vanna_wallet": "authority",
    "vanna_swap": "authority",
    "vanna_risk": "authority",
    "vanna_portfolio": "authority",
    "vanna_plan": "authority",
}
# Actions that take no wallet at all, so one must not be injected.
NO_DEFAULT_ACTIONS: frozenset[tuple[str, str]] = frozenset({
    ("vanna_swap", "quote"), ("vanna_farm_overview", "markets"), ("vanna_risk", "policy"),
})


def signer_param(tool: str, action: str | None) -> str | None:
    """The argument that must equal the caller's wallet for this call, or None if it is not a write."""
    if action is not None and (tool, action) in SIGNER_PARAM:
        return SIGNER_PARAM[(tool, action)]
    return SIGNER_PARAM.get((tool, "*"))


def apply_wallet_scope(
    tool: str, arguments: dict[str, Any], wallet: str, *, is_write: bool
) -> tuple[dict[str, Any], str | None]:
    """Return (arguments, error). Fills the wallet argument when missing; refuses a write for any other wallet."""
    action = arguments.get("action")
    action = action if isinstance(action, str) else None
    out = dict(arguments)

    if is_write:
        param = signer_param(tool, action)
        if param is None:
            # A write we have no rule for must not run for a wallet-scoped caller.
            return out, f"{tool} action={action!r} cannot be used with a wallet login."
        supplied = out.get(param)
        if supplied is None or supplied == "":
            out[param] = wallet
        elif supplied != wallet:
            return out, f"{param} must be your connected wallet ({wallet}); this login can only act as that wallet."
        # For margin_trade the account owner is `authority` in every action except repay_from_wallet,
        # where it names whichever margin account is being repaid; default it to the caller's own.
        default = DEFAULT_PARAM.get(tool)
        if default and out.get(default) in (None, ""):
            out[default] = wallet
        return out, None

    default = DEFAULT_PARAM.get(tool)
    if default and (tool, action or "") not in NO_DEFAULT_ACTIONS and out.get(default) in (None, ""):
        out[default] = wallet
    return out, None
