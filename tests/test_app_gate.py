"""The app's site-wide access gate (backend proxy.ts): app calls log in once and send the cookie."""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest

from vanna_core import app_gate

TOKEN = f"v1.{int((time.time() + 30 * 86_400) * 1000)}.{'a' * 64}"


def _app(state: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"].append(request)
        if request.url.path == "/api/access":
            ok = request.content == b'{"code":"2861"}'
            state["logins"] += 1
            if not ok:
                return httpx.Response(401, json={"ok": False, "error": "wrong"})
            return httpx.Response(200, json={"ok": True}, headers={"Set-Cookie": f"vanna_access={state['token']}; Path=/; HttpOnly"})
        if f"vanna_access={state['valid']}" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"error": "access_required"})
        return httpx.Response(200, json={"ok": True, "path": request.url.path})

    return handler


@pytest.fixture(autouse=True)
def _clear():
    app_gate._cookies.clear()
    yield
    app_gate._cookies.clear()


def _state(token: str = TOKEN) -> dict:
    return {"calls": [], "logins": 0, "token": token, "valid": token}


async def test_logs_in_once_and_reuses_the_cookie() -> None:
    state = _state()
    settings = SimpleNamespace(app_access_code="2861")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_app(state))) as http:
        a = await app_gate.app_request(http, settings, "POST", "https://app.test/api/prices/refresh")
        b = await app_gate.app_request(http, settings, "GET", "https://app.test/api/farm/vaults")
    assert a.status_code == b.status_code == 200
    assert state["logins"] == 1


async def test_a_rotated_cookie_logs_in_again_and_retries() -> None:
    state = _state()
    settings = SimpleNamespace(app_access_code="2861")
    app_gate._cookies["https://app.test"] = ("v1.9999999999999." + "b" * 64, time.time() + 86_400 * 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_app(state))) as http:
        resp = await app_gate.app_request(http, settings, "POST", "https://app.test/api/perps/phoenix-book", json={"market": "SOL"})
    assert resp.status_code == 200
    assert state["logins"] == 1
    assert state["calls"][-1].content == b'{"market":"SOL"}'


async def test_without_a_code_the_gate_answer_is_returned() -> None:
    state = _state()
    settings = SimpleNamespace(app_access_code="")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_app(state))) as http:
        resp = await app_gate.app_request(http, settings, "GET", "https://app.test/api/farm/vaults")
    assert resp.status_code == 401
    assert state["logins"] == 0


async def test_a_wrong_code_returns_the_gate_answer_without_looping() -> None:
    state = _state()
    settings = SimpleNamespace(app_access_code="0000")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_app(state))) as http:
        resp = await app_gate.app_request(http, settings, "GET", "https://app.test/api/farm/vaults")
    assert resp.status_code == 401
    assert state["logins"] == 2  # the first try, then once more after the gate's 401


def test_cookie_expiry_is_read_from_the_token() -> None:
    assert app_gate._expiry("v1.1794382969000.abc") == 1794382969.0
