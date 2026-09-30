"""Vanna Finance (Solana) configuration — settings loaded from environment / .env file.

Uses pydantic-settings for type-safe env var parsing. A singleton is exposed via
get_settings() with @lru_cache so the .env file is read exactly once per process.

There is no registry contract on Solana — the deployed program's own frontend
(`vanna-credit-backend/lib/solana/config.ts`) hardcodes a single program ID,
with no on-chain address resolution. This settings module mirrors that:
VANNA_PROGRAM_ID is a hardcoded default (env-overridable), not resolved at
runtime from anywhere. The asset table itself lives in `vanna_core/assets.py`,
not here — it's static registry data, not a handful of scalar env vars.

Verified against `vanna-credit-backend` @ `f643dbf`, the
`Vanna-Solana-Copilot-Answers.pdf` handoff (2026-09-28) and the owner's
2026-09-29 decisions: target the hosted fork, the connected wallet signs, the app's
keeper prices the fork, only SOL/USDC/USDT are lent/borrowed, liquidation is read-only.
"""

from __future__ import annotations

import logging
from enum import Enum
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class MCPTransport(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"


class OracleMode(str, Enum):
    """Whether health-checked writes refresh fork prices first.

    `dev-fork` (this release): refresh before every health-checked write.
    `pyth-pull`: no refresh — for a real cluster where external keepers publish
    prices. Public-mainnet oracle integration (Hermes) is not built; it is a
    separate, later requirement.
    """

    DEV_FORK = "dev-fork"
    PYTH_PULL = "pyth-pull"


class PriceRefreshSource(str, Enum):
    """Who writes fresh prices onto the fork in `dev-fork` mode."""

    # The app's own price keeper, POST {price_keeper_url}/api/prices/refresh —
    # the owner-designated writer; it also heals stale cloned Kamino reserves.
    KEEPER = "keeper"
    # This server's own surfnet_setAccount adapter (vanna_core/contracts/oracle_refresh.py):
    # fabricates Pyth accounts and restamps Kamino Scope. Only for a fork running a
    # program version the keeper does not cover; never run alongside the keeper.
    CHEATCODE = "cheatcode"


def _is_base58_pubkey(v: str) -> bool:
    try:
        from solders.pubkey import Pubkey

        Pubkey.from_string(v)
        return True
    except Exception:  # noqa: BLE001
        return False


class VannaSettings(BaseSettings):
    """All runtime configuration for the Vanna Solana MCP server.

    Loaded from environment variables or a .env file. Every field has a safe
    hosted-fork default, so local dev needs zero extra config.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Solana RPC — the hosted Surfpool mainnet fork. The "devnet" in the
    # hostname is a legacy naming artifact, not public Solana devnet — see
    # TOOLS_CATALOGUE.md §0/§1: this fork carries real mainnet mint addresses
    # and real Kamino/Jupiter deployments.
    solana_rpc_url: str = "https://rpc-devnet.solana.vanna.finance"
    solana_ws_url: str = "wss://rpc-devnet.solana.vanna.finance"
    # Label surfaced in tool responses / explorer links; not parsed for
    # behaviour except by `is_fork` below.
    solana_cluster_label: str = "surfpool-fork"

    # The ONLY hardcoded on-chain entry point — there is no registry to
    # resolve this from. declare_id!, Anchor.toml and the backend agree; the
    # owner verified the live executable matches their pinned build
    # (2026-09-29); that build is vanna-credit-layer HEAD ecdb9b6.
    vanna_program_id: str = "H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC"

    # Fork price preparation (the app's price keeper; no Hermes this release).
    oracle_mode: OracleMode = OracleMode.DEV_FORK
    price_refresh_source: PriceRefreshSource = PriceRefreshSource.KEEPER
    # The vanna-credit-backend app's HTTP host (NOT the RPC host): POST
    # /api/prices/refresh (price keeper) and POST /api/faucet (vanna_fork).
    # devnet.solana.vanna.finance serves the old stack's API, not this app.
    # Unauthenticated on the app side today; the secret is sent as a Bearer
    # token once the app hardens it.
    price_keeper_url: str = "https://test.solana.vanna.finance"
    price_keeper_secret: str = ""
    # `cheatcode` refresh source only: Kamino's Scope `OraclePrices` account is copied
    # from mainnet onto the fork - the public RPC is rate limited; set your own for reliability.
    mainnet_rpc_url: str = "https://api.mainnet-beta.solana.com"

    # Jupiter v6 (wallet_swap / margin_swap quotes and swap-instruction builds).
    # Keyless requests to lite-api.jup.ag are capped at 0.5 req/s shared
    # across every caller; setting jupiter_api_key switches to the keyed
    # api.jup.ag endpoint with its own quota (verified against
    # vanna-credit-backend/lib/solana/server/jupiter-api.ts). Endpoints are
    # `/swap/v1/quote` and `/swap/v1/swap-instructions` under this base, not
    # the legacy `quote-api.jup.ag/v6` host.
    jupiter_api_key: str = ""
    jupiter_fork_dexes: str = "Raydium,Raydium CLMM,Raydium CP,Orca V2,Meteora DLMM,Whirlpool"

    # Health-factor threshold — informational only. The actual on-chain rule
    # is BALANCE_TO_BORROW_THRESHOLD_WAD, a hardcoded 1.10 program constant
    # (TOOLS_CATALOGUE.md §2.4); this field must never be read as if it could
    # relax that. `safe_health_factor` mirrors the app's own UI policy floor
    # (lib/solana/config.ts SAFE_HEALTH_FACTOR) that pre-flight tools target.
    liquidation_health_factor: float = 1.10
    safe_health_factor: float = 1.15

    # Feature gates — both default OFF; flipping them on is a deliberate,
    # reviewed decision, never a side effect of another setting.
    enable_fork_tools: bool = False
    enable_liquidation_builder: bool = False

    # SHA-256 (LF-normalised) of idl/vanna_lending.json, regenerated from the
    # vanna-credit-layer source (`cargo test --features idl-build`, layer commit ecdb9b6)
    # after the oracle / whole-account-liquidation program update. The MCP refuses
    # to start if the loaded IDL differs (COPILOT_ARCHITECTURE.md B.12 #8).
    pinned_idl_sha256: str = "47cd9ced07539538e42e859b99ba535720a030371399a11ff732b6bc817ffe73"

    # Read cache TTL for hot views (pool stats, oracle price). 0 = disabled.
    read_cache_ttl_seconds: int = 30

    # MCP transport
    mcp_transport: MCPTransport = MCPTransport.STDIO
    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = Field(default=8000, validation_alias="PORT")

    # Logging
    log_level: str = "INFO"

    # ---------------------------------------------------------------------------
    # OAuth 2.1 resource-server protection for streamable-HTTP (mcp_server/auth.py).
    # Off by default so stdio, local dev, and the test suite need no IdP. Any
    # public deployment must set AUTH_ENABLED=true plus the OAUTH_* values.
    # ---------------------------------------------------------------------------
    auth_enabled: bool = False
    oauth_issuer: str = ""
    oauth_jwks_uri: str = ""
    oauth_audience: str = ""
    # Extra accepted audiences (comma-separated), e.g. the app's M2M credential.
    oauth_m2m_audience: str = ""
    # Allowed browser Origin values (comma-separated). Empty = no Origin restriction.
    oauth_allowed_origins: str = ""
    # Externally visible base URL (behind TLS / a proxy), used in discovery
    # metadata and WWW-Authenticate. Empty = derived from the incoming request.
    external_base_url: str = ""
    # Client IDs whose tokens carry no scope claim but are trusted first-party
    # apps (comma-separated). They are granted vanna:read + vanna:write.
    first_party_client_ids: str = ""

    # Per-caller token bucket on POST /mcp (mcp_server/rate_limit.py).
    rate_limit_enabled: bool = True
    rate_limit_refill_per_sec: float = 8.0
    rate_limit_burst: float = 24.0
    # Proxies in front of the server that append to X-Forwarded-For, trusted for the client IP
    # used as the rate-limit key when a call carries no verified token. 0 = ignore the header
    # and use the peer address (safe default). Cloud Run direct: 1; behind an external HTTPS
    # load balancer in front of Cloud Run: 2.
    trusted_proxy_hops: int = Field(default=0, ge=0, le=5)

    @field_validator("vanna_program_id")
    @classmethod
    def validate_pubkey(cls, v: str) -> str:
        if not _is_base58_pubkey(v):
            raise ValueError(f"Expected a base58 Solana address, got: {v!r}")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}, got {v!r}")
        return upper

    @field_validator("rate_limit_refill_per_sec", "rate_limit_burst")
    @classmethod
    def validate_positive(cls, v: float) -> float:
        if not v > 0:
            raise ValueError(f"rate limit values must be positive, got {v!r}")
        return v

    @property
    def is_fork(self) -> bool:
        """Fork-only mechanisms (dev price keeper, faucet, stale-DEX healing)
        are only ever wired up when this is true."""
        return "fork" in self.solana_cluster_label.lower()

    def __repr__(self) -> str:
        try:
            parsed = urlparse(self.solana_rpc_url)
            masked_url = f"{parsed.scheme}://{parsed.netloc}/***"
        except Exception:  # noqa: BLE001
            masked_url = "***"
        return (
            f"VannaSettings("
            f"rpc_url={masked_url!r}, "
            f"cluster={self.solana_cluster_label!r}, "
            f"program={self.vanna_program_id[:8]}..., "
            f"oracle_mode={self.oracle_mode.value!r}, "
            f"transport={self.mcp_transport.value!r}"
            f")"
        )


@lru_cache(maxsize=1)
def get_settings() -> VannaSettings:
    """Return the singleton VannaSettings instance (loaded once per process).

    Call get_settings.cache_clear() in tests to force re-loading from a
    different environment.
    """
    settings = VannaSettings()  # type: ignore[call-arg]
    logger.info("Vanna Solana settings loaded: %r", settings)
    return settings
