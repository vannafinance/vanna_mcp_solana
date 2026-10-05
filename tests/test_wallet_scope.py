"""Offline tests for wallet scoping (mcp_server/wallet_scope.py) against the REAL dispatcher signatures."""

from __future__ import annotations

import inspect
import typing

import pytest
from solders.keypair import Keypair

from mcp_server.dispatchers import ALL_DISPATCHERS
from mcp_server.scopes import WRITE_ACTIONS, is_write_call
from mcp_server.wallet_scope import DEFAULT_PARAM, NO_DEFAULT_ACTIONS, SIGNER_PARAM, apply_wallet_scope, signer_param

DISPATCHERS = {fn.__name__: fn for fn in ALL_DISPATCHERS}
ME = str(Keypair().pubkey())
OTHER = str(Keypair().pubkey())


def _params(tool: str) -> set[str]:
    return set(inspect.signature(DISPATCHERS[tool]).parameters)


def _actions(tool: str) -> set[str]:
    return set(typing.get_args(typing.get_type_hints(DISPATCHERS[tool])["action"]))


def test_every_public_write_action_has_a_signer_rule() -> None:
    """A write the table doesn't name would be refused for wallet logins, but a missed entry should be a test
    failure, not a silently broken tool."""
    for tool, actions in WRITE_ACTIONS.items():
        if tool == "vanna_fork":  # dev-only, never registered on the public service
            continue
        for action in actions:
            assert signer_param(tool, action) is not None, (tool, action)


def test_signer_and_default_params_exist_on_the_dispatchers() -> None:
    for (tool, action), param in SIGNER_PARAM.items():
        assert tool in DISPATCHERS, tool
        assert param in _params(tool), (tool, param)
        if action != "*":
            assert action in _actions(tool), (tool, action)
    for tool, param in DEFAULT_PARAM.items():
        assert tool in DISPATCHERS and param in _params(tool), (tool, param)
    for tool, action in NO_DEFAULT_ACTIONS:
        assert action in _actions(tool), (tool, action)


def test_every_write_action_is_forced_to_the_callers_wallet() -> None:
    for tool, actions in WRITE_ACTIONS.items():
        if tool == "vanna_fork":
            continue
        for action in sorted(actions):
            param = signer_param(tool, action)
            assert param is not None
            # Omitted -> filled with the caller's wallet.
            args, err = apply_wallet_scope(tool, {"action": action}, ME, is_write=True)
            assert err is None and args[param] == ME, (tool, action)
            # Naming yourself is fine.
            args, err = apply_wallet_scope(tool, {"action": action, param: ME}, ME, is_write=True)
            assert err is None and args[param] == ME
            # Naming anyone else is refused.
            _, err = apply_wallet_scope(tool, {"action": action, param: OTHER}, ME, is_write=True)
            assert err is not None and ME in err, (tool, action)


def test_writes_without_a_rule_are_refused_for_wallet_logins() -> None:
    _, err = apply_wallet_scope("vanna_earn_write", {"action": "bogus"}, ME, is_write=True)
    assert err is not None
    _, err = apply_wallet_scope("vanna_new_future_tool", {"action": "x"}, ME, is_write=True)
    assert err is not None


def test_repay_from_wallet_pays_from_the_caller_and_may_repay_anyones_debt() -> None:
    args, err = apply_wallet_scope(
        "vanna_margin_trade", {"action": "repay_from_wallet", "authority": OTHER}, ME, is_write=True
    )
    assert err is None and args["payer"] == ME and args["authority"] == OTHER
    _, err = apply_wallet_scope(
        "vanna_margin_trade", {"action": "repay_from_wallet", "authority": OTHER, "payer": OTHER}, ME, is_write=True
    )
    assert err is not None
    # With no authority at all, the caller repays their own account.
    args, err = apply_wallet_scope("vanna_margin_trade", {"action": "repay_from_wallet"}, ME, is_write=True)
    assert err is None and args["authority"] == ME and args["payer"] == ME


def test_reads_default_to_the_callers_wallet_but_may_name_others() -> None:
    args, _ = apply_wallet_scope("vanna_margin_status", {"action": "health"}, ME, is_write=False)
    assert args["authority"] == ME
    args, _ = apply_wallet_scope("vanna_margin_status", {"action": "health", "authority": OTHER}, ME, is_write=False)
    assert args["authority"] == OTHER
    args, _ = apply_wallet_scope("vanna_earn_position", {"action": "deposited_balance"}, ME, is_write=False)
    assert args["owner"] == ME


def test_wallet_free_reads_get_no_injected_wallet() -> None:
    args, _ = apply_wallet_scope("vanna_swap", {"action": "quote"}, ME, is_write=False)
    assert "authority" not in args
    args, _ = apply_wallet_scope("vanna_farm_overview", {"action": "markets"}, ME, is_write=False)
    assert "authority" not in args
    args, _ = apply_wallet_scope("vanna_oracle", {"action": "price"}, ME, is_write=False)
    assert args == {"action": "price"}


def test_arguments_are_not_mutated() -> None:
    original = {"action": "supply"}
    apply_wallet_scope("vanna_earn_write", original, ME, is_write=True)
    assert original == {"action": "supply"}


def test_write_classification_matches_wallet_rules() -> None:
    """Whatever scopes.py calls a write must go through the signer rule, and nothing else may."""
    for tool, actions in WRITE_ACTIONS.items():
        for action in actions:
            assert is_write_call(tool, action)


@pytest.mark.parametrize("tool", sorted(DISPATCHERS))
def test_no_dispatcher_takes_a_wallet_under_an_unmapped_name(tool: str) -> None:
    """If a dispatcher grows a new owner-style parameter, the table must be taught about it."""
    owner_like = {"authority", "owner", "payer", "wallet", "signer", "fee_payer"}
    known = {p for (t, _), p in SIGNER_PARAM.items() if t == tool} | ({DEFAULT_PARAM[tool]} if tool in DEFAULT_PARAM else set())
    unknown = (_params(tool) & owner_like) - known
    assert not unknown, f"{tool} takes {unknown}, which wallet_scope.py does not handle"
