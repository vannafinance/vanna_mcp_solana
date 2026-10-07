"""The fork's stale-DEX-pool heal around swap builds (vanna_core/fork_heal.py). Offline: the client and the heal are stubbed."""

from __future__ import annotations

from types import SimpleNamespace

from vanna_core import fork_heal
from vanna_core.contracts import swap as swap_mod
from vanna_core.contracts.swap import SwapContract
from vanna_core.envelope import SimulationOutcome, WriteEnvelope


def _envelope(ok: bool, err: str | None = None, logs: list[str] | None = None) -> WriteEnvelope:
    return WriteEnvelope.from_single_transaction(
        cluster="surfpool-fork", program_id="p", fee_payer="w", purpose="jupiter_wallet_swap", unsigned_tx_b64="AA==",
        blockhash="h", last_valid_block_height=1, required_signers=["w"],
        simulation=SimulationOutcome(status="success" if ok else "failed", err=err, logs=logs or []), summary={},
    )


SLIPPAGE = _envelope(False, "InstructionError((2, Custom(6001)))",
                     ["Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 failed: custom program error: 0x1771"])


def test_stale_pool_failures_are_recognised_and_others_are_not():
    assert fork_heal.looks_stale(SLIPPAGE)
    assert fork_heal.looks_stale(_envelope(False, "x", ["Program LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo failed: whatever"]))
    assert fork_heal.looks_stale(_envelope(False, "AccountNotEnoughKeys"))
    assert not fork_heal.looks_stale(_envelope(True))
    assert not fork_heal.looks_stale(_envelope(False, "Program log: insufficient funds"))


def _contract(is_fork: bool) -> SwapContract:
    contract = SwapContract.__new__(SwapContract)
    contract._client = SimpleNamespace(settings=SimpleNamespace(is_fork=is_fork))
    return contract


def _builds(*envelopes):
    calls = []

    async def build():
        calls.append(1)
        return envelopes[min(len(calls), len(envelopes)) - 1]

    return build, calls


async def test_a_healed_stale_swap_is_rebuilt(monkeypatch):
    healed = []

    async def heal(client, tx):
        healed.append(tx)
        return 8

    monkeypatch.setattr(swap_mod, "heal_stale_dex_pools", heal)
    monkeypatch.setattr(swap_mod, "unsigned_transaction", lambda env: object())
    build, calls = _builds(SLIPPAGE, _envelope(True))
    out = await _contract(True)._healing(build)
    assert out.simulation_success and len(calls) == 2 and len(healed) == 1


async def test_rebuilds_stop_after_the_cap(monkeypatch):
    async def heal(client, tx):
        return 3

    monkeypatch.setattr(swap_mod, "heal_stale_dex_pools", heal)
    monkeypatch.setattr(swap_mod, "unsigned_transaction", lambda env: object())
    build, calls = _builds(SLIPPAGE)
    out = await _contract(True)._healing(build)
    assert not out.simulation_success and len(calls) == 1 + fork_heal.MAX_SWAP_HEALS


async def test_no_retry_when_nothing_was_reset_or_off_the_fork(monkeypatch):
    async def heal(client, tx):
        return 0

    monkeypatch.setattr(swap_mod, "heal_stale_dex_pools", heal)
    monkeypatch.setattr(swap_mod, "unsigned_transaction", lambda env: object())
    build, calls = _builds(SLIPPAGE, _envelope(True))
    assert not (await _contract(True)._healing(build)).simulation_success and len(calls) == 1

    async def must_not_heal(client, tx):
        raise AssertionError("healed off the fork")

    monkeypatch.setattr(swap_mod, "heal_stale_dex_pools", must_not_heal)
    build, calls = _builds(SLIPPAGE, _envelope(True))
    assert not (await _contract(False)._healing(build)).simulation_success and len(calls) == 1
