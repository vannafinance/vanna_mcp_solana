"""Offline tests for the price-keeper refresh source: the MCP calls the app's
`POST /api/prices/refresh` instead of writing oracle accounts itself."""

from __future__ import annotations

import httpx
import pytest

from vanna_core.config import OracleMode, PriceRefreshSource, VannaSettings
from vanna_core.contracts import oracle_refresh as refresh

pytestmark = pytest.mark.asyncio


@pytest.fixture
def keeper(monkeypatch: pytest.MonkeyPatch):
    calls: list[httpx.Request] = []
    state = {"status": 200, "body": {"ok": True, "prices": {"SOL": 150.0}, "healedReserves": []}}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(state["status"], json=state["body"])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(refresh.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(refresh, "_keeper_last_refresh", 0.0)

    def fail_adapter(*_a, **_k):
        raise AssertionError("cheatcode adapter must not run when the keeper is the source")

    monkeypatch.setattr(refresh, "DevForkOracleAdapter", fail_adapter)
    supplement_calls: list[object] = []

    async def stub_supplement(client, settings):
        supplement_calls.append(client)
        return []

    monkeypatch.setattr(refresh, "_supplement_uncovered", stub_supplement)
    monkeypatch.setattr(refresh, "_keeper_covered_accounts", set())
    keeper.supplement_calls = supplement_calls  # type: ignore[attr-defined]
    return calls, state


def _settings(**overrides) -> VannaSettings:
    return VannaSettings(price_keeper_url="https://app.example/", **overrides)  # type: ignore[call-arg]


async def test_keeper_is_the_default_source() -> None:
    assert VannaSettings().price_refresh_source == PriceRefreshSource.KEEPER  # type: ignore[call-arg]


async def test_refresh_posts_to_keeper_once_per_window(keeper) -> None:
    calls, _ = keeper
    body = await refresh.maybe_refresh_prices(None, _settings(price_keeper_secret="s3cret"))  # type: ignore[arg-type]
    assert body["ok"] is True
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert str(calls[0].url) == "https://app.example/api/prices/refresh"
    assert calls[0].headers["authorization"] == "Bearer s3cret"

    assert await refresh.maybe_refresh_prices(None, _settings()) is None  # type: ignore[arg-type]
    assert len(calls) == 1


async def test_keeper_error_never_raises_and_is_retried(keeper) -> None:
    calls, state = keeper
    state["status"], state["body"] = 502, {"error": "Jupiter price API down"}
    assert await refresh.maybe_refresh_prices(None, _settings()) is None  # type: ignore[arg-type]
    state["status"], state["body"] = 200, {"ok": True}
    assert (await refresh.maybe_refresh_prices(None, _settings()))["ok"] is True  # type: ignore[arg-type]
    assert len(calls) == 2


async def test_keeper_error_is_raised_by_the_direct_call(keeper) -> None:
    _, state = keeper
    state["status"], state["body"] = 502, {"error": "Jupiter price API down"}
    with pytest.raises(refresh.ContractCallError, match="Jupiter price API down"):
        await refresh.refresh_via_keeper(_settings())


async def test_pyth_pull_mode_skips_refresh(keeper) -> None:
    calls, _ = keeper
    assert await refresh.maybe_refresh_prices(None, _settings(oracle_mode=OracleMode.PYTH_PULL)) is None  # type: ignore[arg-type]
    assert calls == []


async def test_keeper_response_records_which_accounts_it_writes(keeper) -> None:
    _, state = keeper
    state["body"] = {"ok": True, "prices": [{"symbol": "SOL", "account": "AcctSOL"}, {"symbol": "USDC", "account": "AcctUSDC"}]}
    await refresh.refresh_via_keeper(_settings())
    assert refresh._keeper_covered_accounts == {"AcctSOL", "AcctUSDC"}


# --- the Pyth-only supplement (JupUSD): offline, with a fake registry and adapter --------------------

class _Oracle:
    def __init__(self, pyth: str, scope: bool) -> None:
        self.pyth_price, self.uses_scope, self.uses_pyth = pyth, scope, bool(pyth)


def _fake_registry(monkeypatch, assets):
    class FakeRegistry:
        def __init__(self, _client) -> None:
            pass

        async def load(self) -> None:
            pass

        def all(self):
            return assets

    monkeypatch.setattr(refresh, "AssetRegistry", FakeRegistry)


async def test_supplement_writes_only_pyth_only_feeds_the_keeper_does_not(monkeypatch) -> None:
    from types import SimpleNamespace

    assets = [
        SimpleNamespace(oracle=_Oracle("AcctSOL", scope=True)),      # has Scope: not needed
        SimpleNamespace(oracle=_Oracle("AcctJupUSD", scope=False)),  # Pyth-only, keeper does not write it
        SimpleNamespace(oracle=_Oracle("AcctUSDCOnly", scope=False)),  # Pyth-only but the keeper writes it
    ]
    _fake_registry(monkeypatch, assets)
    seen: list[set[str]] = []

    class FakeAdapter:
        def __init__(self, *_a) -> None:
            pass

        async def _refresh_pyth(self, _assets, only=None):
            seen.append(set(only))
            return [{"kind": "pyth", "account": a} for a in only]

        async def _restamp_drifted_scope(self, _assets):
            return []

    monkeypatch.setattr(refresh, "DevForkOracleAdapter", FakeAdapter)
    monkeypatch.setattr(refresh, "_keeper_covered_accounts", {"AcctSOL", "AcctUSDCOnly"})
    monkeypatch.setattr(refresh, "_supplement_last_refresh", 0.0)

    results = await refresh._supplement_uncovered(object(), _settings())  # type: ignore[arg-type]
    assert seen == [{"AcctJupUSD"}]
    assert results == [{"kind": "pyth", "account": "AcctJupUSD"}]

    assert await refresh._supplement_uncovered(object(), _settings()) == []  # type: ignore[arg-type]  # throttled
    assert len(seen) == 1


async def test_supplement_never_raises_and_skips_without_a_client(monkeypatch) -> None:
    assert await refresh._supplement_uncovered(None, _settings()) == []  # type: ignore[arg-type]

    class Boom:
        def __init__(self, _client) -> None:
            raise RuntimeError("rpc down")

    monkeypatch.setattr(refresh, "AssetRegistry", Boom)
    monkeypatch.setattr(refresh, "_supplement_last_refresh", 0.0)
    assert await refresh._supplement_uncovered(object(), _settings()) == []  # type: ignore[arg-type]


# --- Scope clock-drift restamp: pure decision function ---------------------------------------------

def _scope_bytes(entries: dict[int, tuple[int, int]]) -> bytes:
    from vanna_core.pricing import ORACLE_PRICES_DISCRIMINATOR, ORACLE_PRICES_LEN

    data = bytearray(ORACLE_PRICES_LEN)
    data[:8] = ORACLE_PRICES_DISCRIMINATOR
    for entry, (value, ts) in entries.items():
        at = 40 + 56 * entry
        data[at : at + 8] = value.to_bytes(8, "little")
        data[at + 24 : at + 32] = ts.to_bytes(8, "little")
    return bytes(data)


def _scope_asset(chain, twap, max_age):
    from types import SimpleNamespace

    unused = 0xFFFF
    pad = lambda xs: tuple([*xs, *([unused] * (4 - len(xs)))])  # noqa: E731
    return SimpleNamespace(oracle=SimpleNamespace(uses_scope=True, scope_chain=pad(chain), scope_twap_chain=pad(twap), max_age_secs=max_age))


def test_scope_restamp_picks_only_entries_stale_by_fork_clock_but_fresh_in_real_time() -> None:
    wall = 1_800_000_000
    fork = wall + 900  # fork clock 15 min ahead, as observed live
    data = _scope_bytes({
        332: (1, wall - 40),     # xStock: 40 s old really, 940 s by the fork -> restamp
        269: (1, wall - 60),     # its TWAP -> restamp
        3: (1, fork - 5),        # already restamped by the keeper -> leave
        400: (1, wall - 4000),   # a feed that really stopped updating -> must stay stale
        401: (0, wall - 10),     # never written -> not a clock problem
    })
    assets = [_scope_asset([332], [269], 300), _scope_asset([3], [], 120), _scope_asset([400], [401], 300)]
    assert refresh.scope_entries_to_restamp(data, assets, fork, wall) == {332, 269}


def test_scope_restamp_uses_the_strictest_max_age_for_shared_entries() -> None:
    wall = 1_800_000_000
    data = _scope_bytes({3: (1, wall - 200)})  # 200 s old in real time
    # entry 3 is read by an asset allowing 300 s and one allowing 120 s: 200 s is too old for the strict one
    assets = [_scope_asset([3], [], 300), _scope_asset([210, 3], [], 120)]
    assert refresh.scope_entries_to_restamp(data, assets, wall + 900, wall) == set()


def test_scope_restamp_ignores_a_non_scope_account() -> None:
    assert refresh.scope_entries_to_restamp(b"garbage", [_scope_asset([3], [], 120)], 10, 0) == set()
