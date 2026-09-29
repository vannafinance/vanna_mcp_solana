"""`vanna_sign` — confirmation polling for a transaction the user's wallet
already signed and broadcast (TOOLS_CATALOGUE.md §3.14).

Signing is the connected wallet's job, not this server's (DECISIONS.md D3):
the app/Copilot flow is build -> simulate -> wallet signs -> hosted RPC
broadcast -> confirm. There is no remote Sign Service, so there are no
server-side signing or auto-sign actions.
"""

from __future__ import annotations

from typing import Any, Literal

from solders.signature import Signature

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe

SignAction = Literal["tx_status"]


@tool_safe
async def vanna_sign(action: SignAction, signature: str | None = None) -> dict[str, Any]:
    """Poll the confirmation status of a transaction signature.

    Args:
        action: "tx_status" — confirmation status for a signature the user's
            wallet already broadcast.
        signature: Base58 transaction signature.

    On error: {"error": "invalid_input" | "contract_error", "message": "..."}
    """
    if action == "tx_status":
        if signature is None:
            return {"error": "invalid_input", "message": "'signature' is required for action=tx_status"}
        ctx = get_context()
        sig = Signature.from_string(signature)
        status = await ctx.client.get_signature_status(sig)
        if status is None:
            return {"signature": signature, "status": "not_found"}
        return {
            "signature": signature,
            "status": "confirmed" if status.err is None else "failed",
            "confirmations": status.confirmations,
            "confirmation_status": str(status.confirmation_status) if status.confirmation_status else None,
            "err": str(status.err) if status.err else None,
        }
    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_sign. Supported: tx_status",
    }
