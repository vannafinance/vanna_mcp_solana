"""Titan as the second swap provider (vanna_core/contracts/titan.py, swap.py's provider choice). Offline: Titan's and
Jupiter's HTTP answers are faked."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey

from vanna_core.contracts.swap import SwapContract
from vanna_core.contracts.titan import MARGIN_CONFIG, TitanQuote, TitanUnavailableError, margin_swap_call, parse_titan_quote
from vanna_core.phoenix import TITAN_PROGRAM_ID
from vanna_core.types import ContractCallError

MARGIN = Pubkey.new_unique()


def _swap_ix(config: int = 2, first: Pubkey = MARGIN, second: Pubkey = MARGIN, program: Pubkey = TITAN_PROGRAM_ID, n_accounts: int = 14):
    data = bytes([42, config]) + bytes(18) + bytes([1]) + bytes(8)
    accounts = [first, second, *(Pubkey.new_unique() for _ in range(n_accounts - 2))]
    return {"p": str(program), "a": [{"p": str(a), "s": i < 2, "w": True} for i, a in enumerate(accounts)], "d": base64.b64encode(data).decode()}


def _quote(instructions, out=1_000_000, slippage=100) -> TitanQuote:
    return TitanQuote("Titan", 5, out, slippage, instructions, [], ["Whirlpool"])


def test_parse_takes_the_expected_winner_and_its_labels():
    body = {
        "quotes": {"A": {"outAmount": "10", "instructions": []}, "Titan": {"outAmount": "12", "slippageBps": 50, "steps": [{"label": "Orca"}]}},
        "metadata": {"expectedWinner": "Titan"},
    }
    q = parse_titan_quote(body, 1000, 100)
    assert (q.provider, q.out_amount, q.slippage_bps, q.labels, q.in_amount) == ("Titan", 12, 50, ["Orca"], 1000)
    assert parse_titan_quote({"quotes": {"A": {"outAmount": "10"}}}, 1, 100).provider == "A"
    with pytest.raises(ContractCallError):
        parse_titan_quote({"quotes": {}}, 1, 100)


def test_min_out_is_output_less_slippage():
    assert _quote([], out=1_000_000, slippage=100).min_out == 990_000


@pytest.mark.parametrize("config", [0, 1, 2, 3])
def test_margin_call_is_checked_and_config_forced_to_three(config):
    data, accounts = margin_swap_call(_quote([{"p": "ComputeBudget111111111111111111111111111111", "a": [], "d": ""}, _swap_ix(config)]), MARGIN)
    assert data[0] == 42 and data[1] == MARGIN_CONFIG
    assert not any(a.is_signer for a in accounts) and accounts[0].pubkey == MARGIN


@pytest.mark.parametrize(
    "ixs",
    [
        [_swap_ix(), _swap_ix()],  # two swaps
        [_swap_ix(program=Pubkey.new_unique())],  # not Titan
        [_swap_ix(config=4)],  # an unknown config flag
        [_swap_ix(first=Pubkey.new_unique())],  # not built for the margin
        [_swap_ix(n_accounts=6)],  # too few accounts
    ],
)
def test_margin_call_refuses_what_the_validator_would(ixs):
    with pytest.raises(ContractCallError):
        margin_swap_call(_quote(ixs), MARGIN)


def _contract(titan_configured: bool, jupiter_out: int | None, titan_out: int | Exception | None):
    contract = SwapContract.__new__(SwapContract)

    class Jup:
        async def get_quote(self, *a, **k):
            if jupiter_out is None:
                raise ContractCallError("Jupiter has no route")
            return {"outAmount": str(jupiter_out), "otherAmountThreshold": str(jupiter_out - 1), "routePlan": []}

    class Titan:
        configured = titan_configured

        async def get_quote(self, *a, **k):
            if not titan_configured:
                raise TitanUnavailableError("no key")
            if isinstance(titan_out, Exception):
                raise titan_out
            return _quote([], out=titan_out)

    contract._jupiter, contract._titan = Jup(), Titan()
    return contract


ASSETS = (SimpleNamespace(mint="a"), SimpleNamespace(mint="b"))


async def _pick(contract, provider, user_key="w"):
    return await contract._pick(provider, *ASSETS, 100, 100, user_key, "wallet")


async def test_the_better_output_wins_when_no_provider_is_named():
    assert (await _pick(_contract(True, 100, 120), None))[0] == "titan"
    assert (await _pick(_contract(True, 130, 120), None))[0] == "jupiter"


async def test_jupiter_alone_when_titan_cannot_quote():
    assert (await _pick(_contract(False, 100, None), None))[0] == "jupiter"  # no key
    assert (await _pick(_contract(True, 100, ContractCallError("no route")), None))[0] == "jupiter"
    assert (await _pick(_contract(True, 100, 500), None, user_key=None))[0] == "jupiter"  # no wallet to build for


async def test_a_named_provider_is_used_as_asked():
    assert (await _pick(_contract(True, 500, 100), "titan"))[0] == "titan"
    assert (await _pick(_contract(True, 100, 500), "jupiter")) == ("jupiter", None)
    with pytest.raises(TitanUnavailableError):
        await _pick(_contract(False, 100, None), "titan")
    with pytest.raises(ValueError):
        await _pick(_contract(True, 100, 100), "raydium")


async def test_quote_statuses():
    c = _contract(True, 100, ContractCallError("x"))
    assert (await c._quotes(*ASSETS, 100, 100, "w", "wallet"))[2] == "no-route"
    assert (await _contract(False, 100, None)._quotes(*ASSETS, 100, 100, "w", "wallet"))[2] == "not-configured"
    assert (await _contract(True, 100, 1)._quotes(*ASSETS, 100, 100, None, "wallet"))[2] == "no-wallet"


def test_titan_turns_on_with_a_key_or_an_endpoint_url():
    from vanna_core.contracts.titan import TitanClient

    def client(key="", url=""):
        return TitanClient(SimpleNamespace(titan_api_key=key, titan_api_url=url))

    assert not client().configured
    assert client(key="k").configured and client(key="k").base_url == "https://portal.api.titan.exchange/api/v1"
    assert client(url="https://x.quiknode.pro/tok/").base_url == "https://x.quiknode.pro/tok/api/v1"
    assert client(url="https://x.quiknode.pro/tok/api/v1").base_url == "https://x.quiknode.pro/tok/api/v1"


def test_unpadded_base64_data_decodes():
    # Titan sends base64 without padding (Node accepts it; Python must pad).
    from vanna_core.contracts.titan import to_instruction

    ix = to_instruction({"p": str(TITAN_PROGRAM_ID), "a": [], "d": "KgKA8PoCAAAAABQM5hkAAAAAMgACAAAAAgAAAAIAAAHl814kEAEBAAEAypo7FQ"})
    assert bytes(ix.data)[0] == 42
