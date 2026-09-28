"""MCP server entrypoint for Vanna Finance on Solana.

Transports, set via MCP_TRANSPORT env var:
  - "stdio"           — local dev / Claude Desktop / IDE extensions (default)
  - "streamable-http" — hosted / cloud deployments

Lifecycle:
  startup  -> VannaContext.startup() -> SolanaClient connected
  shutdown -> VannaContext.shutdown() -> RPC connection closed

Uses mcp>=2.2.0's `MCPServer` (mcp.server.mcpserver), NOT the legacy 1.x
`mcp.server.fastmcp.FastMCP` API — mcp 2.0 removed FastMCP entirely. This is a
new project so it targets the current SDK rather than pinning to the older
API the sibling Stellar MCP still uses.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.mcpserver import MCPServer

from vanna_core.config import MCPTransport, get_settings
from mcp_server.dependencies import VannaContext, _set_context
from mcp_server.tools import ALL_TOOLS

settings = get_settings()

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_server: MCPServer) -> AsyncIterator[None]:
    ctx = VannaContext(settings)
    await ctx.startup()
    _set_context(ctx)
    try:
        yield
    finally:
        await ctx.shutdown()
        _set_context(None)


mcp = MCPServer(
    name="vanna-solana",
    instructions=(
        "Tools for interacting with the Vanna Finance lending protocol on Solana. "
        "Amounts are human-readable decimal strings. Write tools return an "
        "unsigned, simulated transaction — never sign or submit on the caller's "
        "behalf; hand the unsigned_tx_b64 to the user's own wallet/signing flow."
    ),
    lifespan=_lifespan,
)

for _tool in ALL_TOOLS:
    mcp.add_tool(_tool)


def main() -> None:
    if settings.mcp_transport == MCPTransport.STDIO:
        mcp.run()
    else:
        import uvicorn

        app = mcp.streamable_http_app()
        uvicorn.run(app, host=settings.mcp_http_host, port=settings.mcp_http_port)


if __name__ == "__main__":
    main()
