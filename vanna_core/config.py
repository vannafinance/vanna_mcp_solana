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

from vanna_core.math_utils import BALANCE_TO_BORROW_THRESHOLD_WAD, WAD

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
    # The core program's two agents (vanna-credit-layer origin/main, `vanna_oracle` and `vanna_validator`): the oracle
    # values every holding (a price book keyed by mint), the validator reviews every `margin_execute` call. Both are
    # named in the AssetConfig / Integration accounts, so a redeploy only needs these two overrides.
    vanna_oracle_program_id: str = "FXY5DRfekMUTbUp4uyCFpCZmCccrp6kPq3hTM4GRihnc"
    vanna_validator_program_id: str = "6fND3vhtstp486iE6rsSNVUox3kcjNNRsSLAN7ZPs7th"

    # Fork price preparation (the app's price keeper; no Hermes this release).
    oracle_mode: OracleMode = OracleMode.DEV_FORK
    price_refresh_source: PriceRefreshSource = PriceRefreshSource.KEEPER
    # The vanna-credit-backend app's HTTP host (NOT the RPC host): POST
    # /api/prices/refresh (price keeper) and POST /api/faucet (vanna_fork).
    # devnet.solana.vanna.finance serves the old stack's API, not this app.
    # Unauthenticated on the app side today; the secret is sent as a Bearer
    # token once the app hardens it.
    price_keeper_url: str = "https://test.solana.vanna.finance"
    # Fork only: the app that serves the Phoenix helpers a perps trade needs on a fork (POST /api/perps/phoenix-book to
    # re-copy a market's book from mainnet and get its live mark; POST /api/perps/phoenix-access to stand in for Phoenix's
    # onboarding after a trader is registered). Empty = price_keeper_url. Never called off a fork.
    perps_helper_url: str = ""
    # Optional USD cap on a perps order's size (collateral x leverage); 0 = none, as in the app.
    perps_max_action_usd: float = 0.0
    price_keeper_secret: str = ""
    # The app's site-wide access code (backend proxy.ts / docs/ACCESS_GATE.md): every app `/api/*` call
    # (keeper, Phoenix helpers, Jupiter proxy, vault rows, faucet, Predict) first enters it at POST /api/access
    # for the `vanna_access` cookie (vanna_core/app_gate.py). Empty = no login; a gated app then answers 401.
    app_access_code: str = ""
    # The app's Predict API (World.xyz prediction markets; backend app/api/predict/*). Empty =
    # `{perps_helper_url or price_keeper_url}/api/predict`.
    predict_api_url: str = ""
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
    # Without a key, Jupiter calls go through the app's own proxy routes (`{url}/quote`, `/swap-instructions`,
    # `/swap`; backend app/api/jupiter/*), which call the keyed endpoint with the app's server key - the same way
    # the app's vault actions reach Jupiter. Empty on a fork = `{perps_helper_url or price_keeper_url}/api/jupiter`.
    # Off a fork with no key, the keyless endpoint is used.
    jupiter_proxy_url: str = ""
    jupiter_fork_dexes: str = "Raydium,Raydium CLMM,Raydium CP,Orca V2,Meteora DLMM,Whirlpool"

    # Titan swap API (second swap provider beside Jupiter; vanna-credit-backend lib/solana/server/titan-api.ts).
    # Without a key every swap is quoted and built on Jupiter only, as on the app's Spot page.
    titan_api_key: str = ""
    # Titan's portal (with titan_api_key), or a QuickNode Solana endpoint with the Titan Swap add-on (its token is in
    # the URL, so no key). Either one being set turns Titan on. "/api/v1" is appended when missing.
    titan_api_url: str = ""
    # The fork's frozen clones only serve pool AMMs; prop AMMs (AlphaQ, HumidiFi, SolFi, ...) need state their keepers
    # update every slot. Same list as the Jupiter routes (and the app's TITAN_FORK_DEXES).
    titan_fork_dexes: str = "Raydium,Raydium CLMM,Raydium CP,Orca V2,Meteora DLMM,Whirlpool"
    # Titan's own router only for margin routes: the Vanna validator allows its swap_route_v3 alone.
    titan_providers: str = "Titan"

    # Address lookup table(s) holding the protocol-wide accounts every margin build names (comma-separated).
    # Created and frozen once with scripts/create_protocol_lookup_table.py. Lets large margin swaps fit in
    # one transaction; empty = build without it (a missing table is skipped, never fatal). See protocol_alt.py.
    protocol_lookup_tables: str = ""

    # Whether the program's `margin_execute` still takes a `venue_account` (the GMTrade build, vanna-credit-layer `main`).
    # The liquidation line is the program's BALANCE_TO_BORROW_THRESHOLD_WAD (1.10, `math_utils`; `risk.LIQUIDATION_HF`),
    # never a setting. The product's stricter floor is `min_health_factor` below.

    # Feature gates — both default OFF; flipping them on is a deliberate,
    # reviewed decision, never a side effect of another setting.
    enable_fork_tools: bool = False
    enable_liquidation_builder: bool = False

    # SHA-256 (LF-normalised) of idl/vanna_lending.json, regenerated from the
    # vanna-credit-layer source (`cargo test --features idl-build`, layer commit ecdb9b6)
    # after the oracle / whole-account-liquidation program update. The MCP refuses
    # to start if the loaded IDL differs (a pinned-contract safety rule).
    pinned_idl_sha256: str = "1d019a86781c7e60651a1e49f38d0ad54b6f095d50c07041d47d955fdc3e8445"

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

    # ---------------------------------------------------------------------------
    # Public connector mode (mcp_server/oauth/): this process is its own OAuth authorization server and
    # users log in with their wallet (Sign-In-With-Solana). Only for the separate public service —
    # never set on the Copilot's IAM-locked service, which keeps WorkOS machine-to-machine auth.
    # Requires AUTH_ENABLED=true, EXTERNAL_BASE_URL (the issuer) and OAUTH_CONSENT_URL; refuses to
    # start with FIRST_PARTY_CLIENT_IDS set. See MCP_CONNECTOR_PLAN.md.
    # ---------------------------------------------------------------------------
    public_mode: bool = False
    # The Vanna app page that hosts the wallet-connect consent UI (it only renders; the MCP decides).
    oauth_consent_url: str = ""
    # ES256 private key (PEM) that signs access tokens. Keep in Secret Manager. Empty is refused unless
    # OAUTH_ALLOW_EPHEMERAL_KEY=true (local dev / tests: tokens then die with the process).
    oauth_signing_key_pem: str = ""
    # Retired public keys (PEM, concatenated) still published at the JWKS so tokens signed before a
    # rotation verify until they expire.
    oauth_retired_public_keys_pem: str = ""
    oauth_allow_ephemeral_key: bool = False
    oauth_access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    oauth_refresh_token_ttl_seconds: int = Field(default=30 * 86400, ge=3600)
    oauth_code_ttl_seconds: int = Field(default=60, ge=10, le=300)
    oauth_pending_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    oauth_siws_ttl_seconds: int = Field(default=120, ge=30, le=600)
    # Dynamic client registration caps (per source IP per hour, and across the whole service per day).
    oauth_dcr_per_ip_per_hour: int = Field(default=10, ge=1)
    oauth_dcr_global_per_day: int = Field(default=2000, ge=1)
    # Where the OAuth / plan / safety state lives: `memory` (this process only: local use, one instance) or
    # `firestore` (shared by every instance; required for a scaled public service). See mcp_server/oauth/store.py.
    state_store: str = "memory"
    firestore_project: str = ""
    firestore_collection: str = "mcp_state"

    # ---------------------------------------------------------------------------
    # Product safety layer: the website Copilot's guardrails, applied to every write an MCP client (Claude,
    # Cursor) asks for. In the Copilot these live in its own server code around Gemini; with an MCP client
    # doing the language and planning, they must live here. Always on for the public connector. Off by default
    # for the Copilot's private service (the Copilot enforces them itself) and for the legacy test suite;
    # a local .env turns it on. See mcp_server/safety.py.
    # ---------------------------------------------------------------------------
    safety_layer: bool = False
    # The product's health-factor floor (the program liquidates at 1.10; actions that lower health are held at 1.15).
    min_health_factor: float = Field(default=1.15, gt=BALANCE_TO_BORROW_THRESHOLD_WAD / WAD)
    # USD value of one write / of a UTC day's writes (per wallet).
    max_action_usd: float = Field(default=5_000, gt=0)
    max_daily_usd: float = Field(default=25_000, gt=0)
    # Transaction builds per wallet per minute.
    write_rate_per_minute: int = Field(default=6, ge=1)
    # A plan must be approved within this long of being drawn, and may keep starting steps for the run window.
    plan_approval_ttl_seconds: int = Field(default=300, ge=30)
    plan_run_window_seconds: int = Field(default=1800, ge=60)
    max_plan_steps: int = Field(default=8, ge=1, le=16)

    # Fork test faucet (`vanna_wallet` action=faucet): tops up the caller's own wallet on the test fork. Needs the
    # app's faucet endpoint (price_keeper_url) and a fork cluster; refused anywhere else.
    enable_test_faucet: bool = False
    faucet_requests_per_wallet_per_day: int = Field(default=3, ge=1)

    # Signing links. A write returns, besides the unsigned transaction, a link where the user's own wallet reviews
    # and signs it; the server then relays what the wallet signed. The server never signs. Needs an HTTP transport.
    enable_sign_links: bool = False
    # Origin of the pages above as the user's browser reaches it (e.g. http://127.0.0.1:8000 locally). Defaults to
    # EXTERNAL_BASE_URL, else the local bind address.
    sign_base_url: str = ""
    sign_intent_ttl_seconds: int = Field(default=900, ge=60)

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
