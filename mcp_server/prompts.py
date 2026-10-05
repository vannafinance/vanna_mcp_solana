"""MCP prompts: ready-made starting points a client can offer as slash commands (Claude Code: `/mcp__vanna-solana__lever`).

Each prompt just sets the assistant's role and the right first calls; the rules themselves live in
`agent_firewall.py` and are enforced again by the server.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from mcp_server.agent_firewall import get_agent_instructions


def register_prompts(server: MCPServer, *, public: bool) -> None:
    @server.prompt(name="vanna_assistant", title="Vanna assistant rules",
                   description="The full rules, policy and planning guide for working with Vanna on Solana.")
    def vanna_assistant() -> str:
        return get_agent_instructions(public=public)

    @server.prompt(name="health_check", title="Check my account",
                   description="Show my positions, health factor, how close I am to liquidation, and how much I can safely borrow.")
    def health_check(wallet: str = "") -> str:
        who = f" for wallet {wallet}" if wallet else " for my connected wallet"
        return (
            f"Review my Vanna account{who}. Call vanna_portfolio overview, then vanna_risk liquidation_distance and "
            "vanna_risk capacity (borrow). Summarise positions, health factor, distance to liquidation and safe borrow "
            "capacity in plain language, using only the tool figures."
        )

    @server.prompt(name="lever", title="Lever a position",
                   description="Plan a leveraged long (deposit if needed, borrow a stable, swap inside margin) at the health floor.")
    def lever(asset: str = "SOL", leverage: str = "2", amount: str = "", wallet: str = "") -> str:
        who = f" for wallet {wallet}" if wallet else ""
        amt = f" using {amount} {asset}" if amount else " using my existing margin balance"
        return (
            f"I want to lever {asset} {leverage}x{amt}{who}. First call vanna_risk max_leverage to check what is safe, "
            "then vanna_plan lever (ask me which stable to borrow if it is not obvious). Explain the plan, its worst-case "
            "health factor and the risks, then give me the plan link to approve with my wallet."
        )

    @server.prompt(name="unwind", title="Close everything",
                   description="Plan an exit from all positions in an order where health only improves.")
    def unwind(scope: str = "all", wallet: str = "") -> str:
        who = f" for wallet {wallet}" if wallet else ""
        return (
            f"Close my Vanna positions{who} (scope: {scope}). Call vanna_plan unwind, explain each step and the order, "
            "and give me the plan link to approve with my wallet."
        )

    @server.prompt(name="earn_overview", title="Compare Earn pools and Kamino farm",
                   description="Pool APRs, utilisation, rate curves and Kamino farm APYs, side by side.")
    def earn_overview() -> str:
        return (
            "Compare the Vanna Earn pools (vanna_earn_market pool_stats for SOL, USDC, USDT) and the Kamino farm markets "
            "(vanna_farm_overview markets). Keep Earn and Farm clearly separate. Use only the tool figures."
        )
