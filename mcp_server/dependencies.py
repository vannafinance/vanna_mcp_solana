"""Shared MCP server dependencies — singleton DI container for all tools.

VannaContext wires together the SolanaClient and all contract singletons.
Tools call get_context() to obtain the initialized container; they never
construct clients or contracts themselves.
"""

from __future__ import annotations

import logging

from solders.pubkey import Pubkey

from vanna_core.client import SolanaClient
from vanna_core.config import VannaSettings, get_settings
from vanna_core.contracts.reserve import ReserveContract

logger = logging.getLogger(__name__)

# symbol -> (mint attr on settings, decimals)
_ASSETS: dict[str, tuple[str, int]] = {
    "SOL": ("wsol_mint", 9),
    "USDC": ("usdc_mint", 6),
}


class VannaContext:
    """Singleton DI container for the Vanna Solana MCP server."""

    def __init__(self, settings: VannaSettings) -> None:
        self._settings = settings
        self._client: SolanaClient | None = None
        self._reserves: dict[str, ReserveContract] = {}

    async def startup(self) -> None:
        self._client = SolanaClient(self._settings)
        logger.info("VannaContext started: %r", self._settings)

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.close()

    @property
    def client(self) -> SolanaClient:
        if self._client is None:
            raise RuntimeError("VannaContext not started — call startup() first")
        return self._client

    def normalize_symbol(self, symbol: str) -> str:
        upper = symbol.strip().upper()
        if upper == "WSOL":
            upper = "SOL"
        if upper not in _ASSETS:
            raise ValueError(
                f"{symbol!r} is not a supported asset. Supported: {sorted(_ASSETS)}"
            )
        return upper

    def reserve(self, symbol: str) -> ReserveContract:
        sym = self.normalize_symbol(symbol)
        if sym not in self._reserves:
            mint_attr, decimals = _ASSETS[sym]
            mint = Pubkey.from_string(getattr(self._settings, mint_attr))
            self._reserves[sym] = ReserveContract(self.client, mint, sym, decimals)
        return self._reserves[sym]


_context: VannaContext | None = None


async def init_context(settings: VannaSettings | None = None) -> VannaContext:
    global _context
    ctx = VannaContext(settings or get_settings())
    await ctx.startup()
    _context = ctx
    return ctx


def get_context() -> VannaContext:
    if _context is None:
        raise RuntimeError("VannaContext not initialized — server did not start correctly")
    return _context


def _set_context(ctx: VannaContext | None) -> None:
    """Test-only hook to inject/reset the module-level singleton."""
    global _context
    _context = ctx
