"""One long-lived HTTP connection pool for every outbound call the MCP makes (raw RPC posts such as Phoenix view
simulations, the app's callbacks, Jupiter, Titan, Phoenix and Perena APIs).

A new HTTPS connection to the fork RPC costs ~1.2-1.7 s; a request on a kept connection ~0.3 s. Opening a fresh
`httpx.AsyncClient` per call paid that setup every time, on every tool. `pooled_http(timeout)` hands out the shared
client instead (per event loop, so a test's own loop never sees another's connections), with the caller's timeout
applied to each request. Nothing else changes: same requests, same responses.
"""

from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=32, keepalive_expiry=30.0)  # as the RPC client's
_REQUEST_METHODS = frozenset({"request", "get", "post", "put", "patch", "delete", "head", "options"})

# Per running loop, per client class (tests swap `httpx.AsyncClient` for a mock-transport factory: that gets its own).
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[object, httpx.AsyncClient]]" = weakref.WeakKeyDictionary()


def _client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    factory = httpx.AsyncClient
    per_loop = _clients.setdefault(loop, {})
    client = per_loop.get(factory)
    if client is None or client.is_closed:
        client = factory(limits=_LIMITS)
        per_loop[factory] = client
    return client


class _Pooled:
    """The shared client, with this caller's timeout as the default for each request."""

    def __init__(self, client: httpx.AsyncClient, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    def __getattr__(self, name: str):
        attr = getattr(self._client, name)
        if name not in _REQUEST_METHODS:
            return attr

        def call(*args, **kwargs):
            kwargs.setdefault("timeout", self._timeout)
            return attr(*args, **kwargs)

        return call


@asynccontextmanager
async def pooled_http(timeout: float) -> AsyncIterator[httpx.AsyncClient]:
    """Use as `async with pooled_http(15.0) as http:` where a call used `async with httpx.AsyncClient(timeout=15.0)`.
    Leaving the block keeps the connections open for the next call."""
    yield _Pooled(_client(), timeout)  # type: ignore[misc]
