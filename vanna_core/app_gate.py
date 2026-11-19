"""Calls into the Vanna website (the monorepo Vanna_Finance_Backend, apps/solana) through its site-wide access gate.

The app answers every `/api/*` request without a valid signed `vanna_access` cookie with
`401 {"error": "access_required"}` (`apps/solana/proxy.ts`, docs/ACCESS_GATE.md). The MCP calls the app
server to server (price keeper, Phoenix helpers, Jupiter proxy, vault rows, faucet, Predict), so it
enters the code the way the gate page does: `POST {origin}/api/solana/access {"code": APP_ACCESS_CODE}`
(a standalone app's `/api/access` is tried next) returns the cookie, which is kept per origin until shortly before it expires and sent on every app
call. A 401 `access_required` (cookie rotated, code changed) logs in again once and retries.
Without `APP_ACCESS_CODE` the request goes out as before and a gated app's 401 is returned as is.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

import httpx

from vanna_core.config import VannaSettings

logger = logging.getLogger(__name__)

COOKIE_NAME = "vanna_access"
# Where the gate's login lives: the monorepo website (Vanna_Finance_Backend, the one the MCP calls) serves it at
# /api/solana/access; a standalone app at /api/access is still accepted (the same prefix rule the Jupiter, price-refresh
# and Phoenix-helper calls follow).
_ACCESS_PATHS = ("/api/solana/access", "/api/access")
# Renew this long before the cookie's own expiry (it lasts 30 days).
_RENEW_MARGIN_SECS = 3600.0

# origin -> (cookie value, expiry unix seconds)
_cookies: dict[str, tuple[str, float]] = {}
_login_lock = asyncio.Lock()


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _expiry(token: str) -> float:
    """The token is `v1.<expiresAtMs>.<hmac>`; an unreadable one is renewed after a day."""
    try:
        return int(token.split(".")[1]) / 1000.0
    except (IndexError, ValueError):
        return time.time() + 86_400.0


def _cached(origin: str) -> str | None:
    hit = _cookies.get(origin)
    if hit and hit[1] - _RENEW_MARGIN_SECS > time.time():
        return hit[0]
    return None


async def _login(http: httpx.AsyncClient, origin: str, code: str) -> str | None:
    async with _login_lock:
        cached = _cached(origin)
        if cached:
            return cached
        status = None
        for path in _ACCESS_PATHS:
            try:
                resp = await http.post(f"{origin}{path}", json={"code": code})
            except httpx.HTTPError as exc:
                logger.warning("app access gate: login to %s%s failed: %s", origin, path, exc)
                continue
            token = resp.cookies.get(COOKIE_NAME)
            if resp.status_code == 200 and token:
                _cookies[origin] = (token, _expiry(token))
                return token
            status = resp.status_code
        logger.warning("app access gate: %s refused the access code (HTTP %s)", origin, status)
        return None


def _gated(resp: httpx.Response) -> bool:
    if resp.status_code != 401:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == "access_required"


async def app_request(http: httpx.AsyncClient, settings: VannaSettings, method: str, url: str, **kwargs) -> httpx.Response:
    """`http.request(method, url, **kwargs)` with the app's access cookie; one fresh login and retry on a gate 401."""
    code = (getattr(settings, "app_access_code", "") or "").strip()
    if not code:
        return await http.request(method, url, **kwargs)
    origin = _origin(url)
    base_headers = dict(kwargs.pop("headers", None) or {})
    token = _cached(origin) or await _login(http, origin, code)
    for attempt in range(2):
        headers = {**base_headers, "Cookie": f"{COOKIE_NAME}={token}"} if token else base_headers
        resp = await http.request(method, url, headers=headers, **kwargs)
        if attempt == 1 or not _gated(resp):
            return resp
        _cookies.pop(origin, None)
        token = await _login(http, origin, code)
        if not token:
            return resp
    return resp
