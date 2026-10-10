"""Shared MCP server dependencies — singleton DI container for all dispatchers.

VannaContext wires together the SolanaClient and all contract singletons.
Dispatcher tools call get_context() to obtain the initialized container; they
never construct clients or contracts themselves.
"""

from __future__ import annotations

import asyncio

import logging

from solders.pubkey import Pubkey

from vanna_core.assets import ASSETS, find_asset, assert_lend_borrow_offered
from vanna_core.client import SolanaClient
from vanna_core.config import VannaSettings, get_settings
from vanna_core.contracts.farm import FarmContract
from vanna_core.contracts.farm_kamino import FarmKaminoContract
from vanna_core.contracts.fork import ForkContract
from vanna_core.contracts.history import HistoryContract
from vanna_core.contracts.margin import MarginContract
from vanna_core.contracts.margin_trade import MarginTradeContract
from vanna_core.contracts.oracle import OracleContract
from vanna_core.contracts.protocol import ProtocolContract
from vanna_core.contracts.reserve import ReserveContract
from vanna_core.contracts.swap import SwapContract
from vanna_core.contracts.wallet import WalletContract

logger = logging.getLogger(__name__)


class VannaContext:
    """Singleton DI container for the Vanna Solana MCP server."""

    def __init__(self, settings: VannaSettings) -> None:
        self._settings = settings
        self._client: SolanaClient | None = None
        self._reserves: dict[str, ReserveContract] = {}
        self._protocol: ProtocolContract | None = None
        self._oracle: OracleContract | None = None
        self._margin: MarginContract | None = None
        self._margin_trade: MarginTradeContract | None = None
        self._wallet: WalletContract | None = None
        self._history: HistoryContract | None = None
        self._farm: FarmContract | None = None
        self._farm_kamino: FarmKaminoContract | None = None
        self._swap: SwapContract | None = None
        self._fork: ForkContract | None = None

    async def startup(self) -> None:
        import pathlib

        from vanna_core.client import compute_idl_sha256

        idl_path = pathlib.Path(__file__).resolve().parent.parent / "idl" / "vanna_credit_layer.json"
        actual = compute_idl_sha256(idl_path)
        if actual != self._settings.pinned_idl_sha256:
            raise RuntimeError(
                f"IDL hash mismatch: idl/vanna_credit_layer.json is {actual}, pinned "
                f"{self._settings.pinned_idl_sha256}. Refusing to start — verify the deployed program "
                "matches the new IDL, then update PINNED_IDL_SHA256."
            )
        self._client = SolanaClient(self._settings)
        logger.info("VannaContext started: %r", self._settings)
        # Load the asset registry in the background so the first request finds it cached instead of paying the
        # discovery reads (~10 s on the fork). Best-effort: a failure here only means the first request loads it.
        self._warm_up = asyncio.create_task(self._warm_registry())

    async def _warm_registry(self) -> None:
        from vanna_core.assets import AssetRegistry

        try:
            await AssetRegistry(self._client).load()
        except Exception:  # noqa: BLE001
            logger.warning("registry warm-up failed (the first request loads it instead)", exc_info=True)

    async def shutdown(self) -> None:
        warm = getattr(self, "_warm_up", None)
        if warm is not None and not warm.done():
            warm.cancel()
        if self._client is not None:
            await self._client.close()

    @property
    def client(self) -> SolanaClient:
        if self._client is None:
            raise RuntimeError("VannaContext not started — call startup() first")
        return self._client

    @property
    def settings(self) -> VannaSettings:
        return self._settings

    def normalize_symbol(self, symbol: str) -> str:
        asset = find_asset(symbol)
        if asset is None:
            raise ValueError(
                f"{symbol!r} is not a supported asset. Supported: "
                f"{sorted(a.symbol for a in ASSETS)}"
            )
        return asset.symbol

    def reserve(self, symbol: str) -> ReserveContract:
        asset = find_asset(symbol)
        if asset is None:
            raise ValueError(
                f"{symbol!r} is not a supported asset. Supported: "
                f"{sorted(a.symbol for a in ASSETS)}"
            )
        # Only SOL/USDC/USDT have an Earn pool: other assets are collateral-only and have no Reserve.
        assert_lend_borrow_offered(asset.symbol, "earn")
        if asset.symbol not in self._reserves:
            self._reserves[asset.symbol] = ReserveContract(
                self.client, asset.mint_pubkey, asset.symbol, asset.decimals
            )
        return self._reserves[asset.symbol]

    @property
    def protocol(self) -> ProtocolContract:
        if self._protocol is None:
            self._protocol = ProtocolContract(self.client)
        return self._protocol

    @property
    def oracle(self) -> OracleContract:
        if self._oracle is None:
            self._oracle = OracleContract(self.client)
        return self._oracle

    @property
    def margin(self) -> MarginContract:
        if self._margin is None:
            self._margin = MarginContract(self.client)
        return self._margin

    @property
    def margin_trade(self) -> MarginTradeContract:
        if self._margin_trade is None:
            self._margin_trade = MarginTradeContract(self.client)
        return self._margin_trade

    @property
    def wallet(self) -> WalletContract:
        if self._wallet is None:
            self._wallet = WalletContract(self.client)
        return self._wallet

    @property
    def history(self) -> HistoryContract:
        if self._history is None:
            self._history = HistoryContract(self.client)
        return self._history

    @property
    def farm(self) -> FarmContract:
        if self._farm is None:
            self._farm = FarmContract(self.client)
        return self._farm

    @property
    def farm_kamino(self) -> FarmKaminoContract:
        if self._farm_kamino is None:
            self._farm_kamino = FarmKaminoContract(self.client)
        return self._farm_kamino

    @property
    def swap(self) -> SwapContract:
        if self._swap is None:
            self._swap = SwapContract(self.client)
        return self._swap

    @property
    def perps(self):
        from vanna_core.contracts.perps import PerpsContract

        if getattr(self, "_perps", None) is None:
            self._perps = PerpsContract(self.client)
        return self._perps

    @property
    def vaults(self):
        from vanna_core.contracts.vaults import VaultContract

        if getattr(self, "_vaults", None) is None:
            self._vaults = VaultContract(self.client)
        return self._vaults

    @property
    def fork(self) -> ForkContract:
        if self._fork is None:
            self._fork = ForkContract(self._settings)
        return self._fork


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
