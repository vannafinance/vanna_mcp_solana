"""`vanna_protocol_info` — protocol config, asset registry, integration
whitelist (TOOLS_CATALOGUE.md §3.2). Read-only; admin instructions are
deliberately never wrapped by any tool in this server.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe
from mcp_server.read_cache import cached_read

ProtocolInfoAction = Literal["config", "assets", "integrations"]


def _wrap(field: str, fetch: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[dict[str, Any]]]:
    async def factory() -> dict[str, Any]:
        return {field: await fetch()}

    return factory


@tool_safe
async def vanna_protocol_info(action: ProtocolInfoAction) -> dict[str, Any]:
    """Read-only protocol-wide configuration and registry data.

    Args:
        action: "config" — admin/treasury/operating mode and what it
            currently blocks, max assets per margin, next asset index.
            "assets" — every on-chain-registered asset (plain assets and
            Kamino receipts), with live risk parameters, price source, and
            reserve status. Only SOL, USDC and USDT can be lent or borrowed. "integrations" — which
            external programs `margin_execute` may call right now (Kamino
            Lend, Jupiter), and whether each is enabled.

    On error: {"error": "contract_error" | "invalid_input", "message": "..."}
    """
    ctx = get_context()
    protocol = ctx.protocol
    if action == "config":
        return await cached_read("protocol:config", protocol.get_config)
    if action == "assets":
        return await cached_read("protocol:assets", _wrap("assets", protocol.list_assets))
    if action == "integrations":
        return await cached_read("protocol:integrations", _wrap("integrations", protocol.list_integrations))
    return {
        "error": "invalid_input",
        "message": f"Unknown action {action!r} for vanna_protocol_info. Supported: config, assets, integrations",
    }
