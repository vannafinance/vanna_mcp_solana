"""MCP server entrypoint for Vanna Finance on Solana.

Transports, set via MCP_TRANSPORT env var:
  - "stdio"           — local dev / Claude Desktop / IDE extensions (default)
  - "streamable-http" — hosted / cloud deployments (POST /mcp)

Hosted (streamable-http) composition, outermost first:
  AuthMiddleware (only when AUTH_ENABLED=true) -> RateLimitMiddleware -> MCP app
The MCP app also serves the OAuth discovery routes (always, so clients can
learn the auth configuration) and GET /healthz (also /health: Cloud Run reserves /healthz).

Lifecycle:
  startup  -> VannaContext.startup() -> SolanaClient connected
  shutdown -> VannaContext.shutdown() -> RPC connection closed

Uses mcp>=2.2.0's `MCPServer` (mcp.server.mcpserver), NOT the legacy 1.x
`mcp.server.fastmcp.FastMCP` API — mcp 2.0 removed FastMCP entirely.
"""

from __future__ import annotations

import json
import os
import re
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.context import ServerRequestContext
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolRequestParams, CallToolResult, ListToolsResult, PaginatedRequestParams, TextContent, ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp

from vanna_core.config import MCPTransport, get_settings
from mcp_server.agent_firewall import get_agent_instructions
from mcp_server import safety as safety_layer
from mcp_server.dependencies import VannaContext, _set_context
from mcp_server.dispatchers import ALL_DISPATCHERS, FORK_DISPATCHERS, TOOL_INFO
from mcp_server.identity import caller_has_write_scope, caller_wallet, request_identity
from mcp_server.rate_limit import get_circuit_breaker
from mcp_server.scopes import WRITE_ONLY_TOOLS, is_write_call
from mcp_server.wallet_scope import apply_wallet_scope, signer_param

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
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=None if is_error else payload,
        is_error=is_error,
    )


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


_PYDANTIC_NOISE = re.compile(r"\s*\[type=[^,\]]+(?:, input_value=(?P<value>.*?))?(?:, input_type=[^\]]+)?\]\s*$")


def _condense_validation_error(tool: str, text: str) -> str:
    """'1 validation error for XArguments / action / Input should be ... [type=..., input_value='y']'
    -> "Invalid arguments for X: action: Input should be ... (got 'y')"."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and "errors.pydantic.dev" not in ln]
    parts, field = [], None
    for line in lines[1:]:
        match = _PYDANTIC_NOISE.search(line)
        if match is None:
            field = line
            continue
        message = line[: match.start()]
        got = match.group("value")
        parts.append(f"{field}: {message}" + (f" (got {got})" if got else ""))
    return f"Invalid arguments for {tool}: " + ("; ".join(parts) if parts else lines[0] if lines else text[:200])


def _normalize_sdk_error(tool: str, result: Any) -> Any:
    """Argument-validation and unknown-tool errors are raised by the SDK before a dispatcher runs, as a raw
    `isError` text. Return them in the same `{"error": "invalid_input", ...}` shape the dispatchers use, so a
    client (or model) sees one error contract whatever layer rejected the call."""
    if not getattr(result, "is_error", False) or _result_error_code(result) is not None:
        return result
    text = next((getattr(b, "text", "") for b in getattr(result, "content", None) or [] if getattr(b, "text", "")), "")
    if "validation error" in text:
        # Kept short: the Copilot client parses only the first 300 characters of an isError result.
        return _json_result({"error": "invalid_input", "message": _condense_validation_error(tool, text)[:220]})
    if "Unknown tool" in text:
        return _json_result({"error": "invalid_input", "message": f"Unknown tool {tool!r}."})
    return result


# Tools that run their own safety checks per step (the plan engine pre-flights every step it builds) or are not
# transactions (the faucet has its own per-wallet cap).
_OWN_SAFETY_TOOLS = frozenset({"vanna_plan", "vanna_wallet", "vanna_fork"})
# Single actions of that kind: the Predict CASH faucet (its own per-wallet cap) and the keeper's pool merge (no wallet
# signs it; it moves only what a confirmed trade left in the pool).
_OWN_SAFETY_ACTIONS = frozenset({("vanna_predict", "faucet"), ("vanna_predict", "merge")})


def _own_safety(tool: str, action: Any) -> bool:
    return tool in _OWN_SAFETY_TOOLS or (tool, action) in _OWN_SAFETY_ACTIONS


def _acting_wallet(tool: str, arguments: dict[str, Any]) -> str | None:
    """The wallet a write is for when no login supplies it (local runs): the argument that must sign."""
    action = arguments.get("action")
    param = signer_param(tool, action if isinstance(action, str) else None)
    value = arguments.get(param) if param else None
    return value if isinstance(value, str) and value else None


async def _guard_write(wallet: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Pre-flight a direct write with the same code a plan step gets; {} when it may proceed."""
    from mcp_server.planning.guard import error_for, guard_direct_write

    try:
        result = await guard_direct_write(wallet, tool, arguments)
    except Exception:  # noqa: BLE001 — a pre-flight that cannot run refuses (fail closed)
        logger.warning("safety pre-flight failed | tool=%s", tool, exc_info=True)
        return {"error": "unavailable", "message": "The safety checks could not run just now, so this was not built. Try again."}
    if result.skip:
        return {"error": "invalid_input", "message": result.skip}
    if not result.ok:
        return error_for(result)
    return {}


async def _attach_signing_link(result: Any, tool: str, arguments: dict[str, Any], wallet: str | None) -> Any:
    """Add the plan link (where the user's wallet reviews and signs) to a successful write envelope."""
    from mcp_server.planning.engine import get_engine
    from mcp_server.planning.guard import describe_call

    engine = get_engine()
    if engine is None or getattr(result, "is_error", False):
        return result
    payload = _result_payload(result)
    if not isinstance(payload, dict) or not payload.get("transactions"):
        return result
    acting = wallet or _acting_wallet(tool, arguments) or payload.get("feePayer")
    if not isinstance(acting, str):
        return result
    if payload.get("simulation_success") is not True:
        payload["signing"] = {"available": False, "reason": "The transaction failed its simulation, so there is nothing to sign."}
        return _json_result(payload, is_error=False)
    try:
        view = await engine.create_from_call(acting, tool, arguments, describe_call(tool, arguments))
    except Exception:  # noqa: BLE001 — a link that can't be made must not hide a good build
        logger.warning("could not create signing link", exc_info=True)
        return result
    payload["sign_url"] = view["plan_url"]
    payload["plan_id"] = view["plan_id"]
    payload["signing"] = {
        "available": True, "url": view["plan_url"], "approve_within_seconds": view["approve_within_seconds"],
        "instruction": "Show this link to the user. They open it, approve with their own wallet, and sign. Then call "
                       "vanna_plan action=status with plan_id to confirm; do not say it succeeded before it is confirmed.",
    }
    return _json_result(payload, is_error=False)


def _result_payload(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return dict(structured.get("result", structured))
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return None
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
            is_write = is_write_call(name, action if isinstance(action, str) else None)
            if is_write and not caller_has_write_scope():
                return _json_result({
                    "error": "forbidden",
                    "code": "insufficient_scope",
                    "message": f"{name} action={action!r} requires write scope (vanna:write).",
                })
            wallet = caller_wallet()
            if wallet is not None:
                # Public-connector login: act only as the wallet the token was issued for.
                scoped, refusal = apply_wallet_scope(name, arguments, wallet, is_write=is_write)
                if refusal is not None:
                    return _json_result({"error": "forbidden", "code": "wallet_mismatch", "message": refusal})
                params = params.model_copy(update={"arguments": scoped})
            guarded: dict[str, Any] | None = None
            if is_write and not _own_safety(name, action) and safety_layer.active():
                # The Copilot's guardrails, enforced where the transaction is built (see mcp_server/safety.py).
                acting = wallet or _acting_wallet(name, params.arguments or {})
                if acting is not None:
                    guarded = await _guard_write(acting, name, params.arguments or {})
                    if guarded.get("error"):
                        return _json_result(guarded)
            try:
                result = _normalize_sdk_error(name, await super()._handle_call_tool(ctx, params))
            except Exception:
                breaker.record_failure(name)
                raise
            if is_write and not _own_safety(name, action):
                result = await _attach_signing_link(result, name, params.arguments or {}, wallet)
        if _result_error_code(result) in _DOWNSTREAM_ERRORS:
            breaker.record_failure(name)
        else:
            breaker.record_success(name)
        return result


# Tool definitions, annotations, prompts and instructions only change on a deploy, so clients may reuse what they
# fetched (SEP-2549 cache hints). "private": the tool list differs between read-only and read+write logins.
_FIXED_FOR = CacheHint(ttl_ms=3_600_000, scope="private")

mcp = ScopedMCPServer(
    name="vanna-solana",
    instructions=get_agent_instructions(public=settings.public_mode),
    lifespan=_lifespan,
    cache_hints={"tools/list": _FIXED_FOR, "prompts/list": _FIXED_FOR, "server/discover": _FIXED_FOR},
)

for _dispatcher in ALL_DISPATCHERS:
    _title, _read_only = TOOL_INFO[_dispatcher.__name__]
    mcp.add_tool(_dispatcher, title=_title, annotations=ToolAnnotations(
        title=_title,
        readOnlyHint=_read_only,
        destructiveHint=False,  # writes only build unsigned transactions; the user's wallet signs every one
        idempotentHint=_read_only,
        openWorldHint=True,  # reads and builds against Solana and the Vanna app
    ))

from mcp_server.prompts import register_prompts  # noqa: E402

register_prompts(mcp, public=settings.public_mode)

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
    # Cloud Run's front end answers paths ending in "z" itself (404), so hosted checks use /health.
    ("/health", _healthz),
):
    mcp.custom_route(_path, methods=["GET"])(_handler)


def build_public_app(store: Any = None, server: MCPServer | None = None) -> ASGIApp:
    """The public connector: this process is its own OAuth authorization server (wallet-as-login).

    Composition, outermost first:
      OAuthRateLimitMiddleware (per-IP, unauthenticated OAuth endpoints)
      -> AuthMiddleware (our own ES256 tokens, revocation check; OAuth endpoints exempt)
      -> RateLimitMiddleware (per-wallet, POST /mcp) -> MCP app + OAuth routes.
    `store` defaults to the one `STATE_STORE` names (`memory`: single instance only; `firestore`: shared);
    `server` defaults to the real dispatcher server (tests pass a stub one).
    """
    from mcp_server.auth import AuthMiddleware
    from mcp_server.oauth.http import OAuthRateLimitMiddleware, build_oauth_routes, is_oauth_public_path
    from mcp_server.oauth.keys import KeySet
    from mcp_server.oauth.provider import OAuthConfig, VannaOAuthProvider
    from mcp_server.oauth.store import make_store
    from mcp_server.oauth.verifier import LocalTokenVerifier
    from mcp_server.rate_limit import RateLimitMiddleware
    from mcp_server.web.routes import is_web_public_path

    if not settings.auth_enabled:
        raise RuntimeError("PUBLIC_MODE requires AUTH_ENABLED=true.")
    if settings.first_party_client_ids.strip():
        raise RuntimeError("PUBLIC_MODE must not set FIRST_PARTY_CLIENT_IDS: no public client may be first-party.")
    if settings.enable_fork_tools:
        raise RuntimeError("PUBLIC_MODE must not enable the dev-only fork tools.")

    # On Cloud Run (K_SERVICE is set) an instance can be replaced or duplicated at any moment, so in-process
    # OAuth state would drop logins and let a refresh token be replayed on a second instance.
    if os.environ.get("K_SERVICE") and settings.state_store.strip().lower() != "firestore":
        raise RuntimeError("PUBLIC_MODE on Cloud Run requires STATE_STORE=firestore (shared state across instances).")
    config = OAuthConfig.from_settings(settings)
    keys = KeySet.from_settings(settings)
    if store is None:
        store = make_store(settings)
        if settings.state_store.strip().lower() != "firestore":
            logger.warning("PUBLIC_MODE is using the in-process state store: run a single instance only.")
    provider = VannaOAuthProvider(store, keys, config)
    # The public connector always runs the safety layer and the signing pages (same store as the OAuth state).
    from mcp_server.web.setup import attach_to_app, configure_signing

    configure_signing(settings, store)

    # Stateless: any instance can answer any request, so there is no session to lose on a scale-out or restart.
    app = (server or mcp).streamable_http_app(host=settings.mcp_http_host, stateless_http=True)
    app.router.routes[0:0] = build_oauth_routes(provider)
    attach_to_app(app)

    inner: ASGIApp = app
    if settings.rate_limit_enabled:
        inner = RateLimitMiddleware(
            inner,
            refill_per_sec=settings.rate_limit_refill_per_sec,
            burst=settings.rate_limit_burst,
            trusted_proxy_hops=settings.trusted_proxy_hops,
        )
    allowed_origins = frozenset(o.strip() for o in settings.oauth_allowed_origins.split(",") if o.strip())
    inner = AuthMiddleware(
        app=inner,
        verifier=LocalTokenVerifier(keys, config.issuer, config.resource),  # type: ignore[arg-type]
        allowed_origins=allowed_origins,
        resource_url=config.issuer,
        extra_public_path=lambda path: is_oauth_public_path(path) or is_web_public_path(path),
        claims_check=provider.is_token_active,
    )
    logger.info("Public connector enabled — issuer=%s consent=%s", config.issuer, config.consent_origin)
    return OAuthRateLimitMiddleware(
        inner,
        store,
        register_per_hour=settings.oauth_dcr_per_ip_per_hour,
        trusted_proxy_hops=settings.trusted_proxy_hops,
        plan_proxy_secret=settings.plan_proxy_secret,
    )


def build_streamable_http_app() -> ASGIApp:
    """Compose the hosted ASGI app without binding a socket (testable)."""
    if settings.public_mode:
        return build_public_app()
    from mcp_server.auth import AuthMiddleware, TokenVerifier
    from mcp_server.rate_limit import RateLimitMiddleware

    # `host` is the bind address, not localhost: passing the SDK default
    # (127.0.0.1) auto-enables localhost-only Host/Origin checks, which would
    # reject every request that reaches a hosted deployment by its public name.
    # Origin validation for hosted use is AuthMiddleware's job.
    app: ASGIApp = mcp.streamable_http_app(host=settings.mcp_http_host)
    if settings.enable_sign_links:
        from mcp_server.web.setup import attach_to_app, configure_signing

        configure_signing(settings)
        attach_to_app(app)

    if settings.rate_limit_enabled:
        app = RateLimitMiddleware(
            app,
            refill_per_sec=settings.rate_limit_refill_per_sec,
            burst=settings.rate_limit_burst,
            trusted_proxy_hops=settings.trusted_proxy_hops,
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
            extra_public_path=(lambda p: __import__("mcp_server.web.routes", fromlist=["x"]).is_web_public_path(p))
            if settings.enable_sign_links
            else None,
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
        if settings.enable_sign_links:
            from mcp_server.web.setup import configure_signing, start_local_web_server

            configure_signing(settings)
            start_local_web_server(settings)
        mcp.run()
    else:
        import uvicorn

        uvicorn.run(
            build_streamable_http_app(),
            host=settings.mcp_http_host,
            port=settings.mcp_http_port,
            log_level=settings.log_level.lower(),
            access_log=False,  # an access line would print each plan link (/plan/<id>); the engine logs a plan_ref instead
        )


if __name__ == "__main__":
    main()
