"""Vanna Finance (Solana) Pydantic v2 models and custom exceptions.

All models here are transport-agnostic — they may be serialised to JSON for MCP
tool responses or used internally within vanna_core without modification.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel


def _validate_solana_address(v: str) -> str:
    from solders.pubkey import Pubkey

    try:
        Pubkey.from_string(v)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid Solana address: {v!r}") from exc
    return v


# Base58 Solana address (wallet, mint, PDA — all the same Pubkey encoding).
SolanaAddress = Annotated[str, AfterValidator(_validate_solana_address)]


class VannaCoreError(Exception):
    """Base class for all vanna_core exceptions."""


class ContractCallError(VannaCoreError):
    """A Solana program read (account fetch/decode) failed."""


class SimulationFailedError(VannaCoreError):
    """Transaction simulation was rejected by the RPC node."""

    def __init__(self, message: str, result: "SimulationResult | None" = None) -> None:
        super().__init__(message)
        self.result = result


class InvalidAddressError(VannaCoreError):
    """A malformed Solana address was supplied."""


class StalePriceError(VannaCoreError):
    """The on-chain Pyth price account is too old to trust for a write path."""


class UnhealthyAccountError(VannaCoreError):
    """The requested action would leave (or find) a margin account unhealthy."""


class AccountNotFoundError(VannaCoreError):
    """The requested on-chain account does not exist."""


class AssetNotOfferedError(VannaCoreError):
    """The asset exists on-chain but the product doesn't offer this action for
    it (e.g. lending/borrowing JitoSOL or JupSOL — collateral/swap/farm only)."""


class UnpricablePositionError(VannaCoreError):
    """A position's USD value can't yet be computed (e.g. a Kamino receipt,
    whose price needs `vanna_farm_overview`'s exchange-rate read). Health
    reads must fail closed on this, never silently drop the position."""


class PoolStats(BaseModel):
    """Enriched view of one lending reserve — mirrors `vanna_core.math_utils`
    (which itself mirrors `math/interest.rs`, verified against source).

    NOTE: superseded the old base/slope1/slope2/optimal-utilization display —
    the on-chain reserve uses a `RateCurve` polynomial instead
    (TOOLS_CATALOGUE.md §2.8, §6.1 item "Interest").
    """

    asset_symbol: str
    reserve_address: str
    total_supply: str
    total_borrowed: str
    available_liquidity: str
    utilization_rate_pct: str
    supply_apr_pct: str
    borrow_apr_pct: str
    exchange_rate: str
    reserve_factor_bps: int
    supply_cap: str
    borrow_cap: str
    status: int
    linear_coeff_wad: str
    jump_coeff_wad: str
    rate_multiplier_wad: str


class MarginHealth(BaseModel):
    """Account health — mirrors `math/health.rs::HealthSnapshot`, verified
    against source. One account-wide threshold, no per-asset LTV weighting.

    ``health_factor`` is ``None`` when there is no debt (the on-chain
    sentinel is `u128::MAX`; this is "no debt", never a floating-point
    infinity — see TOOLS_CATALOGUE.md §2.4 / §6.1 item "HF threshold").
    """

    total_collateral_usd: str
    total_debt_usd: str
    health_factor: str | None
    is_healthy: bool
    is_liquidatable: bool


def as_error_dict(exc: BaseException, code: str) -> dict[str, Any]:
    return {"error": code, "message": str(exc)}
