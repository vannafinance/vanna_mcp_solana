"""A Titan margin route is only offered when it fits one transaction (re-quoted under tighter account caps first), and a
pinned one is re-quoted the same way before it is refused."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey

from vanna_core.contracts import swap as swap_mod
from vanna_core.contracts.swap import TITAN_MARGIN_ACCOUNT_CAPS, SwapContract
from vanna_core.types import ContractCallError

WALLET = Pubkey.from_string("4FYMXgPzrvGQxKmbTTkZXfZABKTeUFTuDhyi8W73ETUL")
SOL = SimpleNamespace(symbol="SOL", mint="So11111111111111111111111111111111111111112", decimals=9)
USDC = SimpleNamespace(symbol="USDC", mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", decimals=6)


def contract(monkeypatch) -> SwapContract:
    c = SwapContract.__new__(SwapContract)
    c._client = SimpleNamespace(program_id=Pubkey.from_string("H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC"))

    class Registry:
        def __init__(self, _client):
            pass

        async def load(self):
            return None

    monkeypatch.setattr(swap_mod, "AssetRegistry", Registry)

    async def pair(_f, _t, _r):
        return SOL, USDC, None

    c._pair = pair
    return c


async def test_a_margin_quote_leaves_out_a_titan_route_that_cannot_fit(monkeypatch) -> None:
    c = contract(monkeypatch)
    jq = {"outAmount": "557000000", "otherAmountThreshold": "551000000", "routePlan": []}
    tq = SimpleNamespace(out_amount=557_700_000, min_out=552_100_000, labels=["Titan"])

    async def quotes(*_a):
        return jq, tq, "quoted", None

    async def fits(*_a):
        return False

    caps: list[int | None] = []

    async def titan_quote(*_a, max_accounts=None):
        caps.append(max_accounts)
        return tq

    c._titan = SimpleNamespace(get_quote=titan_quote)
    c._quotes, c._titan_margin_fits = quotes, fits
    q = await c.quote("SOL", "USDC", "5", authority=WALLET, mode="margin")
    assert [p["provider"] for p in q["providers"]] == ["jupiter"] and q["titan_status"] == "too-large"
    # Every tighter cap was tried before Titan was left out.
    assert caps == [c for c in TITAN_MARGIN_ACCOUNT_CAPS if c is not None]

    async def fits_ok(*_a):
        return True

    c._titan_margin_fits = fits_ok
    q = await c.quote("SOL", "USDC", "5", authority=WALLET, mode="margin")
    assert q["provider"] == "titan" and q["titan_status"] == "quoted"
    # A wallet swap has no margin instruction to fit next to: Titan is never checked there.
    c._titan_margin_fits = fits
    assert (await c.quote("SOL", "USDC", "5", authority=WALLET, mode="wallet"))["provider"] == "titan"


async def test_a_pinned_titan_route_is_requoted_then_refused_pointing_to_jupiter(monkeypatch) -> None:
    c = contract(monkeypatch)
    quotes: list[str] = []

    async def get_quote(*_a, max_accounts=None):
        quotes.append(max_accounts)
        return SimpleNamespace()

    c._titan = SimpleNamespace(get_quote=get_quote)
    built: list[int] = []

    async def build(*_a):
        built.append(1)
        return [], [], False

    c._titan_margin_build = build
    with pytest.raises(ContractCallError, match="Jupiter can build this swap"):
        await c._margin_titan(WALLET, WALLET, SOL, USDC, 5_000_000_000, "5", None, SimpleNamespace())
    assert quotes == list(TITAN_MARGIN_ACCOUNT_CAPS[1:]) and len(built) == len(TITAN_MARGIN_ACCOUNT_CAPS)

    # A re-quoted route that fits is built.
    attempts = iter([False, True])

    async def build_second(*_a):
        return ["ix"], [], next(attempts)

    async def envelope(*_a, **_k):
        return "envelope"

    c._titan_margin_build = build_second
    c._client = SimpleNamespace(program_id=c._client.program_id, build_write_envelope=envelope)
    quote = SimpleNamespace(out_amount=1, min_out=1)
    c._titan = SimpleNamespace(get_quote=lambda *_a, **_k: _async(quote))
    assert await c._margin_titan(WALLET, WALLET, SOL, USDC, 5_000_000_000, "5", None, quote) == "envelope"


async def test_a_route_that_fits_only_under_a_cap_is_offered(monkeypatch) -> None:
    c = contract(monkeypatch)
    jq = {"outAmount": "557000000", "otherAmountThreshold": "551000000", "routePlan": []}
    wide = SimpleNamespace(out_amount=557_900_000, min_out=552_300_000, labels=["Titan"], accounts=40)
    tight = SimpleNamespace(out_amount=557_700_000, min_out=552_100_000, labels=["Titan"], accounts=20)

    async def quotes(*_a):
        return jq, wide, "quoted", None

    async def titan_quote(*_a, max_accounts=None):
        return tight

    async def fits(_auth, _f, _t, _r, quote):
        return quote.accounts <= 20

    c._quotes, c._titan_margin_fits = quotes, fits
    c._titan = SimpleNamespace(get_quote=titan_quote)
    q = await c.quote("SOL", "USDC", "5", authority=WALLET, mode="margin")
    assert q["titan_status"] == "quoted" and q["provider"] == "titan" and q["out_amount"] == "557.7"


async def _async(v):
    return v
