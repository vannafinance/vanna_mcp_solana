"""Vanna Finance (Solana) configuration — settings loaded from environment / .env file.

Uses pydantic-settings for type-safe env var parsing. A singleton is exposed via
get_settings() with @lru_cache so the .env file is read exactly once per process.

Unlike the Stellar/Soroban sibling MCP, there is NO registry contract on Solana —
the deployed program's frontend (Backend-Solana repo, lib/solana/config.ts) hardcodes
a single program ID and a static asset table directly, with no on-chain address
resolution. This settings module mirrors that: VANNA_PROGRAM_ID and the asset table
are hardcoded defaults (env-overridable), not resolved at runtime from anywhere.
See VANNA_SOLANA_MCP_REQUIREMENTS.md (Backend-Solana repo) Part A/B for the open
questions this still depends on (canonical program ID for the target deployment,
final asset list, sign-service interface, etc).
"""

from __future__ import annotations

import logging
from enum import Enum
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class SolanaCluster(str, Enum):
    DEVNET = "devnet"
    TESTNET = "testnet"
    MAINNET = "mainnet"


class MCPTransport(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"


class S2SAuthMode(str, Enum):
    """How the MCP server authenticates to the Sign Service (service-to-service)."""

    SHARED_SECRET = "shared_secret"
    OIDC = "oidc"


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
    devnet default — matching Backend-Solana's own .env.example — so local dev
    needs zero extra config beyond a Pyth API key for write paths.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Solana RPC
    solana_rpc_url: str = "https://api.devnet.solana.com"
    solana_cluster: SolanaCluster = SolanaCluster.DEVNET

    # The ONLY hardcoded on-chain entry point — there is no registry to resolve
    # this from. See VANNA_SOLANA_MCP_REQUIREMENTS.md Part A #1 (Backend-Solana repo).
    vanna_program_id: str = "BZ812nUv4Qhr2p1JVgmoJGjYTGk1brAXckyhFSCNH3Zg"

    # Static asset table (SOL, USDC only for v1 — matches lib/solana/config.ts
    # ASSETS in Backend-Solana). See Part A #2 for whether this grows before ship.
    usdc_mint: str = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
    wsol_mint: str = "So11111111111111111111111111111111111111112"

    # Pyth Hermes (price refresh before any write path that needs a fresh
    # price_update account: borrow, withdraw_collateral, deposit_and_borrow,
    # liquidate). Requires authentication as of 2026-08-26.
    pyth_hermes_url: str = "https://pyth.dourolabs.app/hermes"
    pyth_api_key: str = ""
    pyth_feed_id_sol: str = "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d"
    pyth_feed_id_usdc: str = "eaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a"

    # Health / liquidation threshold — single value for all assets today.
    # See VANNA_SOLANA_MCP_REQUIREMENTS.md Part A #6 for whether this is final.
    liquidation_threshold: float = 1.1

    # Read cache TTL for hot views (pool stats, oracle price). 0 = disabled.
    read_cache_ttl_seconds: int = 30

    # MCP transport
    mcp_transport: MCPTransport = MCPTransport.STDIO
    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = Field(default=8000, validation_alias="PORT")

    # Logging
    log_level: str = "INFO"

    # ---------------------------------------------------------------------------
    # Sign Service (zero-custody signing delegate)
    #
    # The MCP server holds NO private keys and signs NOTHING. Every write-path
    # tool returns an unsigned transaction + simulation result; the
    # vanna_sign_and_submit tool forwards it to the Sign Service, which owns
    # the actual signer. These settings only describe how to REACH and
    # AUTHENTICATE to that service. See Part A #3 for the exact request/response
    # schema it expects — unconfigured here (sign_service_url="") until that's
    # answered.
    # ---------------------------------------------------------------------------
    sign_service_url: str = ""
    sign_service_secret: str = ""
    s2s_auth_mode: S2SAuthMode = S2SAuthMode.SHARED_SECRET

    @field_validator("vanna_program_id", "usdc_mint", "wsol_mint")
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

    @model_validator(mode="after")
    def _validate_pyth_feed_ids(self) -> "VannaSettings":
        for name, feed_id in (
            ("pyth_feed_id_sol", self.pyth_feed_id_sol),
            ("pyth_feed_id_usdc", self.pyth_feed_id_usdc),
        ):
            hex_part = feed_id.removeprefix("0x")
            if len(hex_part) != 64:
                raise ValueError(f"{name} must be a 32-byte hex string, got: {feed_id!r}")
        return self

    def __repr__(self) -> str:
        try:
            parsed = urlparse(self.solana_rpc_url)
            masked_url = f"{parsed.scheme}://{parsed.netloc}/***"
        except Exception:  # noqa: BLE001
            masked_url = "***"
        return (
            f"VannaSettings("
            f"rpc_url={masked_url!r}, "
            f"cluster={self.solana_cluster.value!r}, "
            f"program={self.vanna_program_id[:8]}..., "
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
