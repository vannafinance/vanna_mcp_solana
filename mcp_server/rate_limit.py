"""Per-caller token-bucket rate limit on POST /mcp, plus a per-tool circuit breaker.

Cost is weighted by what the JSON-RPC body asks for: a write action costs 10,
an expensive read 5, anything else 1 (see mcp_server/scopes.py). Buckets are
process-local, so each instance enforces its own ceiling; a fleet-wide limit
would need a shared store.

The caller key is, in order: the verified `sub`, the verified client ID, the
first X-Forwarded-For hop, the peer address. It sits inside AuthMiddleware, so
the verified identity is already stamped on the request scope.

Ported from the Stellar MCP's rate_limit.py without its first-party per-user
two-level buckets, which key on Stellar wallet addresses; add them once the
Solana Copilot's forwarded-subject header is defined.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import OrderedDict, defaultdict

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_server.identity import identity_from_scope
from mcp_server.scopes import EXPENSIVE_READ_TOOLS, is_write_call

logger = logging.getLogger(__name__)

DEFAULT_TOOL_COST = 1
EXPENSIVE_READ_COST = 5
WRITE_COST = 10
_MAX_BUCKETS = 4096
_IDLE_TTL_SEC = 300.0


def tool_cost(tool: str, action: str | None = None) -> int:
    if is_write_call(tool, action):
        return WRITE_COST
    if tool in EXPENSIVE_READ_TOOLS:
        return EXPENSIVE_READ_COST
    return DEFAULT_TOOL_COST


class TokenBucket:
    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.tokens = capacity
        self.updated_at = time.monotonic()
        self.last_accessed_at = self.updated_at

    def _refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.refill_per_sec)
        self.updated_at = now

    def take(self, cost: float, now: float | None = None) -> float:
        """Debit `cost` and return 0, or return the seconds to wait (no debit)."""
        current = time.monotonic() if now is None else now
        self._refill(current)
        self.last_accessed_at = current
        deficit = cost - self.tokens
        if deficit > 0:
            return deficit / self.refill_per_sec
        self.tokens -= cost
        return 0.0

    def is_full_and_idle(self, now: float) -> bool:
        self._refill(now)
        return self.tokens >= self.capacity and now - self.last_accessed_at >= _IDLE_TTL_SEC


class CircuitBreaker:
    """Opens a tool for `cool_sec` after `fail_threshold` consecutive downstream failures."""

    def __init__(self, fail_threshold: int = 5, cool_sec: float = 30.0) -> None:
        self.fail_threshold = fail_threshold
        self.cool_sec = cool_sec
        self.failures: dict[str, int] = defaultdict(int)
        self.open_until: dict[str, float] = {}

    def allow(self, tool: str) -> bool:
        until = self.open_until.get(tool)
        if until is None:
            return True
        if time.monotonic() >= until:
            self.open_until.pop(tool, None)
            self.failures[tool] = 0
            return True
        return False

    def record_success(self, tool: str) -> None:
        self.failures[tool] = 0
        self.open_until.pop(tool, None)

    def record_failure(self, tool: str) -> None:
        self.failures[tool] += 1
        if self.failures[tool] >= self.fail_threshold:
            self.open_until[tool] = time.monotonic() + self.cool_sec
            logger.warning("Circuit open for %s for %.0fs", tool, self.cool_sec)

    def retry_after_seconds(self, tool: str) -> float:
        return max(0.0, self.open_until.get(tool, 0.0) - time.monotonic())


_GLOBAL_BREAKER = CircuitBreaker()


def get_circuit_breaker() -> CircuitBreaker:
    return _GLOBAL_BREAKER


def rpc_tool_call(body: bytes) -> tuple[str, str | None]:
    """(tool name, action) of a tools/call body; ("", None) for anything else."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", None
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return "", None
    params = payload.get("params")
    if not isinstance(params, dict):
        return "", None
    name = params.get("name")
    arguments = params.get("arguments")
    action = arguments.get("action") if isinstance(arguments, dict) else None
    return (name if isinstance(name, str) else "", action if isinstance(action, str) else None)


def caller_key(scope: Scope) -> str:
    stamped = identity_from_scope(scope)
    claims = stamped[0] if stamped else None
    if claims:
        sub = claims.get("sub")
        if isinstance(sub, str) and sub:
            return f"sub:{sub[:64]}"
        cid = claims.get("client_id") or claims.get("azp")
        if isinstance(cid, str) and cid:
            return f"client:{cid[:64]}"
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    client = scope.get("client")
    if client:
        return f"ip:{client[0]}"
    return "anon"


class RateLimitMiddleware:
    """ASGI token bucket on POST /mcp; every other route passes through."""

    def __init__(self, app: ASGIApp, *, refill_per_sec: float = 8.0, burst: float = 24.0) -> None:
        if not math.isfinite(refill_per_sec) or refill_per_sec <= 0:
            raise ValueError("refill_per_sec must be positive")
        if not math.isfinite(burst) or burst < WRITE_COST:
            raise ValueError(f"burst must admit the maximum tool cost ({WRITE_COST})")
        self.app = app
        self._refill_per_sec = refill_per_sec
        self._burst = burst
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._overflow = TokenBucket(burst, refill_per_sec)

    def _bucket(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is not None:
            self._buckets.move_to_end(key)
            return bucket
        now = time.monotonic()
        for idle in [k for k, b in self._buckets.items() if b.is_full_and_idle(now)]:
            del self._buckets[idle]
        if len(self._buckets) >= _MAX_BUCKETS:
            # Every tracked caller is active; new keys share one bounded bucket
            # rather than evicting (and so resetting) a depleted caller.
            return self._overflow
        bucket = TokenBucket(self._burst, self._refill_per_sec)
        self._buckets[key] = bucket
        return bucket

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", "")).rstrip("/") or "/"
        if scope["type"] != "http" or scope.get("method") != "POST" or path != "/mcp":
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)

        tool, action = rpc_tool_call(body)
        cost = tool_cost(tool, action) if tool else DEFAULT_TOOL_COST
        key = caller_key(scope)
        wait = self._bucket(key).take(cost)
        if wait > 0:
            logger.warning("Rate limited caller=%s tool=%s cost=%s", key, tool or "-", cost)
            await _send_429(send, math.ceil(wait))
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _send_429(send: Send, retry_after: int) -> None:
    content = json.dumps(
        {"error": "rate_limited", "message": "Too many MCP calls. Wait a moment and retry."}
    ).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(content)).encode("ascii")),
        (b"retry-after", str(max(1, retry_after)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": 429, "headers": headers})
    await send({"type": "http.response.body", "body": content, "more_body": False})
