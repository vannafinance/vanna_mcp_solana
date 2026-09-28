"""MCP error handling — maps vanna_core exceptions to clean AI-readable responses.

The @tool_safe decorator wraps every tool function. It catches VannaCoreError
subclasses and returns a structured error dict; raw RPC/simulation internals
are never leaked past the tool boundary.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from vanna_core.types import (
    AccountNotFoundError,
    ContractCallError,
    InvalidAddressError,
    SimulationFailedError,
    StalePriceError,
    UnhealthyAccountError,
    VannaCoreError,
)

logger = logging.getLogger(__name__)

_ERROR_MAP: list[tuple[type[VannaCoreError], str]] = [
    (StalePriceError, "stale_price"),
    (UnhealthyAccountError, "health_check_failed"),
    (SimulationFailedError, "simulation_failed"),
    (InvalidAddressError, "invalid_address"),
    (AccountNotFoundError, "account_not_found"),
    (ContractCallError, "contract_error"),
    (VannaCoreError, "vanna_error"),
]


def _error_code(exc: VannaCoreError) -> str:
    for exc_type, code in _ERROR_MAP:
        if isinstance(exc, exc_type):
            return code
    return "vanna_error"


def _is_context_not_ready_error(exc: RuntimeError) -> bool:
    msg = str(exc).lower()
    return "vannacontext" in msg or "not initialized" in msg or "not started" in msg


def tool_safe(fn: Any) -> Any:
    """Decorator that catches all exceptions and returns structured error dicts.

    Return shape on error::

        {"error": "<code>", "message": "<human-readable description>"}

    Error codes: stale_price, health_check_failed, simulation_failed,
    invalid_address, account_not_found, contract_error, vanna_error,
    invalid_input (ValueError), server_not_ready, internal_error.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except VannaCoreError as exc:
            code = _error_code(exc)
            logger.warning("Tool %r raised %s: %s", fn.__name__, type(exc).__name__, exc)
            return {"error": code, "message": str(exc)}
        except ValueError as exc:
            logger.warning("Tool %r raised ValueError: %s", fn.__name__, exc)
            return {"error": "invalid_input", "message": str(exc)}
        except RuntimeError as exc:
            if _is_context_not_ready_error(exc):
                logger.error("Tool %r called before VannaContext ready: %s", fn.__name__, exc)
                return {
                    "error": "server_not_ready",
                    "message": "The MCP server is still starting. Retry in a few seconds.",
                }
            logger.error("Tool %r raised unexpected RuntimeError", fn.__name__, exc_info=True)
            return {"error": "internal_error", "message": "An unexpected error occurred."}
        except Exception:  # noqa: BLE001
            logger.error("Tool %r raised unexpected exception", fn.__name__, exc_info=True)
            return {"error": "internal_error", "message": "An unexpected error occurred."}

    return wrapper
