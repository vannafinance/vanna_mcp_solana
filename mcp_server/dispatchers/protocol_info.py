"""`vanna_protocol_info` — protocol config, asset registry, integration
whitelist (TOOLS_CATALOGUE.md §3.2). Read-only; admin instructions are
deliberately never wrapped by any tool in this server.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

from mcp_server.dependencies import get_context
from mcp_server.error_handling import tool_safe
from mcp_server.read_cache import cached_read

ProtocolInfoAction = Literal["config", "assets", "integrations", "concepts"]

CONCEPTS: dict[str, str] = {
    "health_factor": "Your health factor (HF) is your margin collateral's USD value divided by your debt's USD value. Above 1.10 the protocol leaves you alone; at or below 1.10 your account can be liquidated. The product keeps every action at 1.15 or above, and with no debt there is no health factor.",
    "liquidation": "Liquidation is whole-account: when HF falls to 1.10 or below, a liquidator can repay all your debt and take all your collateral. I can tell you whether you are close, but I never execute a liquidation. Repaying debt or adding collateral raises your HF.",
    "leverage": "Leverage here means borrowing a stable asset (USDC or USDT) against your collateral, swapping it into the asset you want more of, and keeping that as collateral. '2x' means exposure of twice your equity. The planner sizes it so your health factor stays at or above 1.15 even if the swap fills at its worst allowed price.",
    "collateral": "Collateral is what you deposit into your margin account (SOL, USDC, USDT, JitoSOL, JupSOL or Kamino receipts). Borrowed funds also sit in the margin account and count as collateral. You can only withdraw collateral while your health factor stays safe.",
    "earn": "Earn is the Vanna lending pool: supply SOL, USDC or USDT and earn the pool's supply rate, redeemable any time there is available liquidity. It is separate from your margin account.",
    "farm": "Farm supplies a single asset to Kamino lend from your margin balance and gives you a Kamino receipt token (kSOL, kUSDC, ...) that still counts as collateral. Redeeming converts it back.",
    "margin": "Your margin account is one account per wallet. You deposit collateral, borrow SOL, USDC or USDT against it, and repay or withdraw. Borrowing is limited by your health factor.",
    "general": "Vanna on Solana lets you earn on SOL/USDC/USDT, borrow against collateral in a margin account, swap with Jupiter, and farm on Kamino. Everything is built for your own wallet to sign.",
}


def _wrap(field: str, fetch: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[dict[str, Any]]]:
    async def factory() -> dict[str, Any]:
        return {field: await fetch()}

    return factory


@tool_safe
async def vanna_protocol_info(action: ProtocolInfoAction, topic: str | None = None) -> dict[str, Any]:
    """Read-only protocol-wide configuration and registry data.

    Args:
        action: "config" — admin/treasury/operating mode and what it
            currently blocks, max assets per margin, next asset index.
            "assets" — every on-chain-registered asset (plain assets and
            Kamino receipts), with live risk parameters, price source, and
            reserve status. Only SOL, USDC and USDT can be lent or borrowed. "integrations" — which
            external programs `margin_execute` may call right now (Kamino
            Lend, Jupiter), and whether each is enabled. "concepts" — plain-language
            explanations to relay (static text, no live figures): `topic` is one of
            health_factor, liquidation, leverage, collateral, earn, farm, margin, general
            (omit for all).
        topic: For "concepts" only.

    On error: {"error": "contract_error" | "invalid_input", "message": "..."}
    """
    if action == "concepts":
        if topic is None:
            return {"concepts": CONCEPTS}
        if topic not in CONCEPTS:
            return {"error": "invalid_input", "message": f"Unknown topic {topic!r}. Topics: {', '.join(CONCEPTS)}"}
        return {"topic": topic, "explanation": CONCEPTS[topic]}
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
        "message": f"Unknown action {action!r} for vanna_protocol_info. Supported: config, assets, integrations, concepts",
    }
