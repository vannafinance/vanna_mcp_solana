"""The product safety layer: the website Copilot's guardrails, enforced server-side for MCP-client callers.

In the Copilot, Gemini only understands language; plain code decides amounts, checks risk, applies limits and
refuses. When Claude or Cursor replaces Gemini, the model's reading of the user is still fallible and a prompt can
be injected, so the same rules must be enforced by this server where the transaction is built — not left to
instructions the model may ignore:

  * the 1.15 health-factor floor on every action that can lower health (the program only enforces 1.10);
  * a USD cap per action and per UTC day, and a build rate per minute, keyed by the caller's wallet;
  * amounts the model typed are validated, never rounded or reinterpreted;
  * Only SOL/USDC/USDT can be lent and borrowed; only the five product assets exist.

Limits are counters in the shared record store (the same one the OAuth server uses), so they hold across
instances once that store is shared. They fail closed: a counter that cannot be read refuses the write.

`active()` says when the layer applies: always for a wallet login (public connector) and wherever SAFETY_LAYER=true
(a local run). The Copilot's private service keeps enforcing its own copy and is left untouched.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from mcp_server.oauth.store import MemoryRecordStore, RecordStore
from vanna_core.config import get_settings

logger = logging.getLogger(__name__)

_MICRO = 1_000_000


@dataclass
class LimitResult:
    ok: bool
    kind: str = ""
    message: str = ""
    retry_after_s: int = 0

    def to_error(self) -> dict[str, Any]:
        return {"error": "limit_exceeded", "kind": self.kind, "message": self.message, "retry_after_seconds": self.retry_after_s}


def _utc_day(now: float) -> int:
    return int(now // 86400)


def _seconds_to_utc_midnight(now: float) -> int:
    return max(1, int((_utc_day(now) + 1) * 86400 - now))


class Safety:
    def __init__(self, store: RecordStore | None = None, clock=time.time) -> None:
        self.store = store or MemoryRecordStore()
        self._clock = clock

    # ------------------------------------------------------------------ build rate

    async def consume_write(self, wallet: str) -> LimitResult:
        """One transaction build against the per-minute budget."""
        limit = get_settings().write_rate_per_minute
        now = self._clock()
        count = await self.store.incr(f"safety:write:{wallet}:{int(now // 60)}", 60)
        if count > limit:
            retry = max(1, int((int(now // 60) + 1) * 60 - now))
            return LimitResult(
                False, "rate", f"Too many transaction builds (limit {limit} per minute). Try again in {retry}s.", retry
            )
        return LimitResult(True)

    # ------------------------------------------------------------------ spend

    async def check_spend(self, wallet: str, usd: float) -> LimitResult:
        """One action's USD value against the per-action and per-day caps; does not record it."""
        s = get_settings()
        if not (usd == usd) or usd < 0 or usd == float("inf"):
            return LimitResult(False, "spend", "The action's USD value could not be determined, so it is not allowed.")
        if usd > s.max_action_usd:
            return LimitResult(
                False, "spend",
                f"This action is worth about ${usd:,.2f}, above the per-action limit of ${s.max_action_usd:,.0f}. "
                "Try a smaller amount.",
            )
        now = self._clock()
        record = await self.store.get(f"safety:spend:{wallet}:{_utc_day(now)}")
        spent = (int(record["n"]) if record else 0) / _MICRO
        if spent + usd > s.max_daily_usd:
            return LimitResult(
                False, "spend",
                f"This would bring today's total to about ${spent + usd:,.2f}, above the daily limit of "
                f"${s.max_daily_usd:,.0f}.",
                _seconds_to_utc_midnight(now),
            )
        return LimitResult(True)

    async def record_spend(self, wallet: str, usd: float) -> None:
        if not (usd == usd) or usd <= 0 or usd == float("inf"):
            return
        now = self._clock()
        await self.store.incr(f"safety:spend:{wallet}:{_utc_day(now)}", 2 * 86400, by=round(usd * _MICRO))

    # ------------------------------------------------------------------ faucet

    async def consume_faucet(self, wallet: str) -> LimitResult:
        limit = get_settings().faucet_requests_per_wallet_per_day
        now = self._clock()
        count = await self.store.incr(f"safety:faucet:{wallet}:{_utc_day(now)}", 2 * 86400)
        if count > limit:
            return LimitResult(
                False, "faucet",
                f"The test faucet allows {limit} top-ups per wallet per day. Try again after 00:00 UTC.",
                _seconds_to_utc_midnight(now),
            )
        return LimitResult(True)


_safety: Safety | None = None


def get_safety() -> Safety:
    global _safety
    if _safety is None:
        _safety = Safety()
    return _safety


def set_safety(safety: Safety | None) -> None:
    global _safety
    _safety = safety


def active() -> bool:
    """True when this process must enforce the product safety layer on writes."""
    s = get_settings()
    return bool(s.safety_layer or s.public_mode)
