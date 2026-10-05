"""HTTP surface of the connector's authorization server.

Composes the SDK's protocol routes (`/register`, `/authorize`, `/token`, `/revoke`, AS metadata) with
this service's own: the JWKS and the wallet-login consent API. Also holds the pre-authentication rate
limiter, since none of these paths carry a bearer token to key a bucket on.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Awaitable, Callable

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_server.auth import build_protected_resource_metadata
from mcp_server.oauth.consent import ConsentError, ConsentService
from mcp_server.oauth.provider import VALID_SCOPES, VannaOAuthProvider
from mcp_server.oauth.store import RecordStore
from mcp_server.rate_limit import caller_key

logger = logging.getLogger(__name__)

JWKS_PATH = "/.well-known/jwks.json"
CONSENT_PREFIX = "/oauth/consent"
MAX_BODY_BYTES = 8 * 1024

# Paths that need no bearer token: they ARE the login. Each is protected by its own proof
# (PKCE, a single-use request id plus a wallet signature) and by the pre-auth rate limiter below.
PUBLIC_PATHS = frozenset({"/register", "/authorize", "/token", "/revoke", JWKS_PATH})
PUBLIC_PREFIXES = (CONSENT_PREFIX + "/",)

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def is_oauth_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


# ---------------------------------------------------------------------------
# Pre-authentication rate limiting (per client IP, fixed window, shared via the store)
# ---------------------------------------------------------------------------


class OAuthRateLimitMiddleware:
    """Per-IP limits on the unauthenticated OAuth endpoints. Everything else passes through."""

    def __init__(
        self,
        app: ASGIApp,
        store: RecordStore,
        *,
        register_per_hour: int = 10,
        authorize_per_minute: int = 60,
        token_per_minute: int = 60,
        consent_per_minute: int = 60,
        trusted_proxy_hops: int = 0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.app = app
        self._store = store
        self._hops = trusted_proxy_hops
        self._clock = clock or time.time
        # path -> (bucket name, limit, window seconds)
        self._rules: dict[str, tuple[str, int, int]] = {
            "/register": ("register", register_per_hour, 3600),
            "/authorize": ("authorize", authorize_per_minute, 60),
            "/token": ("token", token_per_minute, 60),
            "/revoke": ("token", token_per_minute, 60),
        }
        self._consent_rule = ("consent", consent_per_minute, 60)
        self._plan_rule = ("plan", 120, 60)

    def _rule(self, path: str) -> tuple[str, int, int] | None:
        if path in self._rules:
            return self._rules[path]
        if path.startswith(PUBLIC_PREFIXES):
            return self._consent_rule
        if path.startswith("/api/plan/"):
            return self._plan_rule
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        rule = self._rule(str(scope.get("path", "")).rstrip("/") or "/")
        if rule is None:
            await self.app(scope, receive, send)
            return
        name, limit, window = rule
        now = self._clock()
        bucket = int(now // window)
        count = await self._store.incr(f"rl:{name}:{caller_key(scope, self._hops)}:{bucket}", window)
        if count > limit:
            retry_after = max(1, math.ceil((bucket + 1) * window - now))
            response = JSONResponse(
                {"error": "rate_limited", "message": "Too many requests. Try again shortly."},
                status_code=429,
                headers={"Retry-After": str(retry_after), **_NO_STORE},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _cors_post(handler: Callable[[Request], Awaitable[Response]]) -> Any:
    """The SDK's own CORS wrapper for token-style endpoints (clients that run in a browser, e.g. an inspector)."""
    return cors_middleware(handler, ["POST", "OPTIONS"])


def _json(payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={**_NO_STORE, **(headers or {})})


def build_oauth_routes(provider: VannaOAuthProvider) -> list[Route]:
    config = provider.config
    consent = ConsentService(provider)
    # Parsed through AuthSettings so a path-less issuer keeps its exact form: RFC 8414 compares the metadata
    # `issuer` to the token `iss` as plain strings, and a spurious trailing slash would break every client.
    issuer: AnyHttpUrl = AuthSettings(
        issuer_url=config.issuer, resource_server_url=config.resource, validate_token_resource=False
    ).issuer_url
    registration = ClientRegistrationOptions(
        enabled=True, valid_scopes=list(VALID_SCOPES), default_scopes=list(VALID_SCOPES)
    )
    revocation = RevocationOptions(enabled=True)

    # The SDK's metadata advertises only client-secret auth; public (PKCE-only) clients are accepted here, so
    # say so, or spec-following clients would register for a secret they do not need.
    metadata = build_metadata(issuer, None, registration, revocation)
    metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
    metadata.revocation_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
    metadata_handler = MetadataHandler(metadata).handle

    sdk_routes = [
        r
        for r in create_auth_routes(provider, issuer, None, registration, revocation)
        if r.path not in ("/.well-known/oauth-authorization-server", "/revoke")
    ]

    # RFC 7009 revocation. The SDK's handler insists on a `client_secret` field even for public clients
    # (which have none), so a spec-following public client could never revoke. Same logic, secret optional.
    authenticator = ClientAuthenticator(provider)

    async def revoke(request: Request) -> Response:
        try:
            client = await authenticator.authenticate_request(request)
        except AuthenticationError:
            return JSONResponse({"error": "unauthorized_client"}, status_code=401, headers=_NO_STORE)
        form = await request.form()
        token = form.get("token")
        if not isinstance(token, str) or not token:
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=_NO_STORE)
        candidate: Any = await provider.load_refresh_token(client, token) or await provider.load_access_token(token)
        # A token belonging to another client is left alone; an unknown token is a silent success (RFC 7009 §2.2).
        if candidate is not None and candidate.client_id == client.client_id:
            await provider.revoke_token(candidate)
        return Response(status_code=200, headers=_NO_STORE)

    async def as_metadata(request: Request) -> Response:
        return await metadata_handler(request)

    async def resource_metadata(_request: Request) -> Response:
        # RFC 9728. This service is its own authorization server and does accept the vanna:* scopes.
        return JSONResponse(
            build_protected_resource_metadata(
                resource_url=config.resource, issuer=config.issuer, scopes_supported=list(VALID_SCOPES)
            )
        )

    async def jwks(_request: Request) -> Response:
        return JSONResponse(provider.keys.jwks(), headers={"Cache-Control": "public, max-age=300"})

    # -- consent API (called by the Vanna app's consent page from the browser) --

    def _cors(request: Request) -> dict[str, str]:
        origin = request.headers.get("origin")
        if origin == config.consent_origin:
            return {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Headers": "content-type",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Vary": "Origin",
            }
        return {}

    def _origin_ok(request: Request, *, required: bool) -> bool:
        origin = request.headers.get("origin")
        if origin is None:
            return not required
        return origin == config.consent_origin

    async def _body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise ConsentError("invalid_request", "Request body too large.", 413)
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ConsentError("invalid_request", "Body must be JSON.") from exc
        if not isinstance(data, dict):
            raise ConsentError("invalid_request", "Body must be a JSON object.")
        return data

    def consent_route(
        handler: Callable[[Request], Awaitable[dict[str, Any]]], *, method: str
    ) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            if request.method == "OPTIONS":
                return Response(status_code=204, headers=_cors(request))
            cors = _cors(request)
            if not _origin_ok(request, required=method == "POST"):
                return _json({"error": "forbidden", "message": "Origin not allowed."}, 403)
            try:
                return _json(await handler(request), headers=cors)
            except ConsentError as exc:
                return _json({"error": exc.code, "message": exc.message}, exc.status, cors)
            except Exception:
                logger.exception("consent endpoint failed")
                return _json({"error": "server_error", "message": "Something went wrong."}, 500, cors)

        return endpoint

    async def describe(request: Request) -> dict[str, Any]:
        return await consent.describe(request.query_params.get("req", ""))

    async def challenge(request: Request) -> dict[str, Any]:
        body = await _body(request)
        return await consent.challenge(str(body.get("req", "")), str(body.get("wallet", "")), body.get("scopes"))

    async def complete(request: Request) -> dict[str, Any]:
        body = await _body(request)
        redirect = await consent.complete(
            str(body.get("req", "")), str(body.get("wallet", "")), body.get("nonce", ""), body.get("signature", "")
        )
        return {"redirect_url": redirect}

    async def deny(request: Request) -> dict[str, Any]:
        body = await _body(request)
        return {"redirect_url": await consent.deny(str(body.get("req", "")))}

    consent_routes = [
        Route(f"{CONSENT_PREFIX}/request", consent_route(describe, method="GET"), methods=["GET", "OPTIONS"]),
        Route(f"{CONSENT_PREFIX}/challenge", consent_route(challenge, method="POST"), methods=["POST", "OPTIONS"]),
        Route(f"{CONSENT_PREFIX}/complete", consent_route(complete, method="POST"), methods=["POST", "OPTIONS"]),
        Route(f"{CONSENT_PREFIX}/deny", consent_route(deny, method="POST"), methods=["POST", "OPTIONS"]),
    ]

    return [
        Route("/.well-known/oauth-authorization-server", as_metadata, methods=["GET", "OPTIONS"]),
        Route("/.well-known/oauth-authorization-server/{resource:path}", as_metadata, methods=["GET", "OPTIONS"]),
        Route("/.well-known/oauth-protected-resource", resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/{resource:path}", resource_metadata, methods=["GET"]),
        Route(JWKS_PATH, jwks, methods=["GET"]),
        Route("/revoke", _cors_post(revoke), methods=["POST", "OPTIONS"]),
        *sdk_routes,
        *consent_routes,
    ]
