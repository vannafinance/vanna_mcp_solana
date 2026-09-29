"""MCP server entrypoint for Vanna Finance on Solana.

Transports, set via MCP_TRANSPORT env var:
  - "stdio"           — local dev / Claude Desktop / IDE extensions (default)
  - "streamable-http" — hosted / cloud deployments (POST /mcp)

Hosted (streamable-http) composition, outermost first:
  AuthMiddleware (only when AUTH_ENABLED=true) -> RateLimitMiddleware -> MCP app
The MCP app also serves the OAuth discovery routes (always, so clients can
learn the auth configuration) and GET /healthz.

Lifecycle:
  startup  -> VannaContext.startup() -> SolanaClient connected
  shutdown -> VannaContext.shutdown() -> RPC connection closed

Uses mcp>=2.2.0's `MCPServer` (mcp.server.mcpserver), NOT the legacy 1.x
`mcp.server.fastmcp.FastMCP` API — mcp 2.0 removed FastMCP entirely.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolRequestParams, CallToolResult, ListToolsResult, PaginatedRequestParams, TextContent
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp

from vanna_core.config import MCPTransport, get_settings
from mcp_server.agent_firewall import get_agent_instructions
from mcp_server.dependencies import VannaContext, _set_context
from mcp_server.dispatchers import ALL_DISPATCHERS, FORK_DISPATCHERS
from mcp_server.identity import caller_has_write_scope, request_identity
from mcp_server.rate_limit import get_circuit_breaker
from mcp_server.scopes import WRITE_ONLY_TOOLS, is_write_call

settings = get_settings()

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

# tool_safe error codes that mean a dependency (RPC, Jupiter, price keeper)
# failed rather than the caller's input — these feed the circuit breaker.
_DOWNSTREAM_ERRORS = frozenset({"internal_error"})


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


def _json_result(payload: dict[str, Any], *, is_error: bool = True) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], is_error=is_error)


def _result_error_code(result: Any) -> str | None:
    """The `error` field of a dispatcher's dict result, if it returned one."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        payload = structured.get("result", structured)
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            return payload["error"]
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            return payload["error"]
    return None


class ScopedMCPServer(MCPServer):
    """MCPServer that binds the per-request caller identity around every tool
    handler, hides/refuses write actions without write scope, and applies the
    per-tool circuit breaker.

    Identity is read from `ServerRequestContext.request` (the HTTP request that
    carried this JSON-RPC message), not a ContextVar from the middleware — see
    mcp_server/identity.py for why that distinction matters under stateful
    streamable HTTP.
    """

    async def _handle_list_tools(
        self, ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        with request_identity(ctx.request):
            tools = await self.list_tools()
            if not caller_has_write_scope():
                tools = [t for t in tools if t.name not in WRITE_ONLY_TOOLS]
        return ListToolsResult(tools=tools)

    async def _handle_call_tool(self, ctx: ServerRequestContext[Any], params: CallToolRequestParams) -> Any:
        name = params.name
        arguments = params.arguments or {}
        action = arguments.get("action")
        breaker = get_circuit_breaker()
        with request_identity(ctx.request):
            if not breaker.allow(name):
                return _json_result({
                    "error": "tool_circuit_open",
                    "message": f"{name} is temporarily degraded after repeated downstream failures. "
                    f"Retry in {max(1, round(breaker.retry_after_seconds(name)))}s.",
                })
            if is_write_call(name, action if isinstance(action, str) else None) and not caller_has_write_scope():
                return _json_result({
                    "error": "forbidden",
                    "code": "insufficient_scope",
                    "message": f"{name} action={action!r} requires write scope (vanna:write).",
                })
            try:
                result = await super()._handle_call_tool(ctx, params)
            except Exception:
                breaker.record_failure(name)
                raise
        if _result_error_code(result) in _DOWNSTREAM_ERRORS:
            breaker.record_failure(name)
        else:
            breaker.record_success(name)
        return result


mcp = ScopedMCPServer(
    name="vanna-solana",
    instructions=get_agent_instructions(),
    lifespan=_lifespan,
)

for _dispatcher in ALL_DISPATCHERS:
    mcp.add_tool(_dispatcher)

if settings.enable_fork_tools:
    logger.warning("ENABLE_FORK_TOOLS=true — registering dev-only fork tools (never for production/Copilot use)")
    for _dispatcher in FORK_DISPATCHERS:
        mcp.add_tool(_dispatcher)


# ---------------------------------------------------------------------------
# Public HTTP routes (bypass auth — see mcp_server/auth.py is_public_path)
# ---------------------------------------------------------------------------


def _base_url(request: Request) -> str:
    return settings.external_base_url.rstrip("/") or str(request.base_url).rstrip("/")


async def _protected_resource_metadata(request: Request) -> Response:
    from mcp_server.auth import build_protected_resource_metadata

    return JSONResponse(
        build_protected_resource_metadata(
            resource_url=f"{_base_url(request)}/mcp",
            issuer=settings.oauth_issuer or "UNCONFIGURED — set OAUTH_ISSUER env var",
        )
    )


async def _authorization_server_metadata(request: Request) -> Response:
    # This is a resource server, but some MCP clients probe the AS metadata on
    # the resource origin during Dynamic Client Registration; point them at the IdP.
    issuer = settings.oauth_issuer.rstrip("/")
    if not issuer:
        return JSONResponse(
            {"error": "server_error", "detail": "OAUTH_ISSUER is not configured on this server."},
            status_code=500,
        )
    return RedirectResponse(f"{issuer}/.well-known/oauth-authorization-server", status_code=302)


async def _healthz(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


for _path, _handler in (
    ("/.well-known/oauth-protected-resource", _protected_resource_metadata),
    ("/.well-known/oauth-protected-resource/{resource:path}", _protected_resource_metadata),
    ("/.well-known/oauth-authorization-server", _authorization_server_metadata),
    ("/.well-known/oauth-authorization-server/{resource:path}", _authorization_server_metadata),
    ("/healthz", _healthz),
):
    mcp.custom_route(_path, methods=["GET"])(_handler)


def build_streamable_http_app() -> ASGIApp:
    """Compose the hosted ASGI app without binding a socket (testable)."""
    from mcp_server.auth import AuthMiddleware, TokenVerifier
    from mcp_server.rate_limit import RateLimitMiddleware

    # `host` is the bind address, not localhost: passing the SDK default
    # (127.0.0.1) auto-enables localhost-only Host/Origin checks, which would
    # reject every request that reaches a hosted deployment by its public name.
    # Origin validation for hosted use is AuthMiddleware's job.
    app: ASGIApp = mcp.streamable_http_app(host=settings.mcp_http_host)

    if settings.rate_limit_enabled:
        app = RateLimitMiddleware(
            app, refill_per_sec=settings.rate_limit_refill_per_sec, burst=settings.rate_limit_burst
        )

    if settings.auth_enabled:
        missing = [
            name
            for name, value in (
                ("OAUTH_ISSUER", settings.oauth_issuer),
                ("OAUTH_JWKS_URI", settings.oauth_jwks_uri),
                ("OAUTH_AUDIENCE", settings.oauth_audience),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"AUTH_ENABLED=true but {', '.join(missing)} not set")
        allowed_origins = frozenset(o.strip() for o in settings.oauth_allowed_origins.split(",") if o.strip())
        audiences = [settings.oauth_audience]
        audiences += [a.strip() for a in settings.oauth_m2m_audience.split(",") if a.strip()]
        app = AuthMiddleware(
            app=app,
            verifier=TokenVerifier(
                issuer=settings.oauth_issuer, jwks_uri=settings.oauth_jwks_uri, audience=audiences
            ),
            allowed_origins=allowed_origins,
            resource_url=settings.external_base_url
            or f"http://{settings.mcp_http_host}:{settings.mcp_http_port}",
        )
        logger.info(
            "Auth enabled — issuer=%s audience=%s allowed_origins=%s",
            settings.oauth_issuer,
            audiences,
            sorted(allowed_origins) or "(any)",
        )
    else:
        logger.warning(
            "AUTH_ENABLED=false — authentication is disabled. "
            "Set AUTH_ENABLED=true and the OAUTH_* vars for any public deployment."
        )
    return app


def main() -> None:
    if settings.mcp_transport == MCPTransport.STDIO:
        mcp.run()
    else:
        import uvicorn

        uvicorn.run(
            build_streamable_http_app(),
            host=settings.mcp_http_host,
            port=settings.mcp_http_port,
            log_level=settings.log_level.lower(),
        )


if __name__ == "__main__":
    main()
