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


class SimulationResult(BaseModel):
    """Result of simulating a built transaction against the RPC node."""

    success: bool
    error: str | None = None
    logs: list[str] = []
    units_consumed: int | None = None


class UnsignedTransaction(BaseModel):
    """A built-and-simulated, unsigned transaction ready for a signing service.

    ``unsigned_tx_b64`` is the base64-encoded serialized message (legacy or
    versioned, per builder). The MCP server never signs this — see
    VANNA_SOLANA_MCP_REQUIREMENTS.md Part A #3 for the signing-service contract
    this gets handed to.
    """

    unsigned_tx_b64: str
    fee_payer: str
    instruction_name: str
    program_id: str
    simulation_success: bool
    simulation_error: str | None
    units_consumed: int | None = None


class PoolStats(BaseModel):
    """Enriched view of one lending reserve — mirrors lib/pool-stats.ts."""

    asset_symbol: str
    reserve_address: str
    total_supply: str
    total_borrowed: str
    available_liquidity: str
    utilization_rate_pct: str
    supply_apy_pct: str
    borrow_apy_pct: str
    exchange_rate: str
    reserve_factor_bps: int
    base_rate_bps: int
    slope1_bps: int
    slope2_bps: int
    optimal_utilization_bps: int


class MarginHealth(BaseModel):
    """Derived health/solvency figures — mirrors lib/margin-health.ts."""

    health_factor: float
    collateral_left_before_liquidation_usd: float
    net_available_collateral_usd: float
    debt_limit_usd: float
    total_value_usd: float


def as_error_dict(exc: BaseException, code: str) -> dict[str, Any]:
    return {"error": code, "message": str(exc)}
