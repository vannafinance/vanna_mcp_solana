"""OAuth 2.1 resource-server protection for the Vanna Solana MCP server.

RESOURCE SERVER ONLY: validates tokens issued by an external IdP. No token
issuance, login, or consent flow lives here.

Disabled by default (AUTH_ENABLED=false). A hosted deployment sets:
    AUTH_ENABLED=true
    OAUTH_ISSUER=<IdP issuer URL>
    OAUTH_JWKS_URI=<IdP JWKS URL>
    OAUTH_AUDIENCE=<expected audience>

Standards: OAuth 2.1 resource server (JWT + JWKS), RFC 6750 bearer usage,
RFC 9728 protected-resource metadata, MCP Streamable HTTP Origin validation.

Ported from the Stellar MCP (Vanna_MCP/vanna-mcp/mcp_server/auth.py) minus its
WorkOS-email -> Privy-DID resolution through the Stellar Sign Service: on
Solana the user's connected wallet signs and there is no Sign Service
(the connected wallet signs; there is no Sign Service), so that hop has no counterpart.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio
import jwt
from jwt import PyJWKClient, PyJWKClientError
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_server.identity import bind_identity_to_scope, reset_identity, set_identity

logger = logging.getLogger(__name__)

USER_ASSERTION_HEADER = "x-vanna-user-assertion"

# OAuth discovery documents must be reachable without a token, or a client can
# never learn where to get one. Matched exactly and with a trailing path
# segment (the RFC 8414 §3.1 / RFC 9728 §3.1 path-aware form).
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
)

_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]


_PUBLIC_EXACT_PATHS = frozenset({"/healthz"})


def is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT_PATHS:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _PUBLIC_PATH_PREFIXES)


class AuthError(Exception):
    """Token validation failure. Always fails closed."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TokenVerifier:
    """Validates bearer JWTs against an IdP's JWKS: key -> signature -> exp -> iss -> aud."""

    def __init__(self, issuer: str, jwks_uri: str, audience: str | list[str]) -> None:
        # IdPs stamp `iss` without a trailing slash even when the configured
        # base URL has one; PyJWT compares it by exact string.
        self._issuer = issuer.rstrip("/")
        self._audience = [audience] if isinstance(audience, str) else list(audience)
        self._jwks_client = PyJWKClient(jwks_uri, cache_keys=True)

    def _signing_key(self, token: str) -> Any:
        return self._jwks_client.get_signing_key_from_jwt(token).key

    def verify(self, token: str) -> dict[str, Any]:
        try:
            key = self._signing_key(token)
        except (PyJWKClientError, jwt.PyJWTError, ValueError, UnicodeDecodeError) as exc:
            logger.warning("Token key lookup failed: %s", exc)
            raise AuthError(f"Invalid token: {exc}") from exc
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud"]},
                leeway=30,
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("Invalid token audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("Invalid token issuer") from exc
        except jwt.PyJWTError as exc:
            logger.warning("JWT validation failed: %s", exc)
            raise AuthError(f"Token validation failed: {exc}") from exc
        return claims


def _is_machine_token(claims: dict[str, Any]) -> bool:
    """A client-credentials token names the client as its own subject."""
    sub = claims.get("sub")
    cid = claims.get("client_id") or claims.get("azp")
    return isinstance(sub, str) and isinstance(cid, str) and sub == cid


class AuthMiddleware:
    """ASGI middleware enforcing bearer authentication on everything but OAuth discovery.

    - Origin present and not allowed -> 403 (DNS-rebinding / CSRF protection).
      An absent Origin is allowed: server-to-server callers don't send one.
    - Missing / malformed / invalid bearer -> 401 with WWW-Authenticate pointing
      at the protected-resource metadata.
    - Valid bearer -> the verified claims and the end-user assertion are stamped
      onto the request scope (see mcp_server/identity.py).

    End-user assertion: the bearer says which CLIENT is calling; an app holding
    one M2M credential for all its users sends the user's own token in
    X-Vanna-User-Assertion. Precedence is that header, then the bearer itself
    unless it is a machine token, which would make every user look like one
    subject. It is carried unverified for per-user features only; it never
    authorizes a transaction — the user's wallet signature does.
    """

    def __init__(
        self,
        app: ASGIApp,
        verifier: TokenVerifier,
        allowed_origins: frozenset[str],
        resource_url: str,
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._allowed_origins = allowed_origins
        self._resource_url = resource_url.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http" or is_public_path(str(scope.get("path", ""))):
            await self._app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

        origin = headers.get("origin")
        if origin and self._allowed_origins and origin not in self._allowed_origins:
            logger.warning("Origin %r not in allowed origins %s", origin, sorted(self._allowed_origins))
            await _send_json(send, status=403, body={"error": "forbidden", "detail": "Origin not allowed"})
            return

        auth_header = headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            await self._send_401(send, "Bearer token required")
            return
        token = auth_header[7:].strip()

        # PyJWKClient may fetch the JWKS synchronously on a cache miss.
        try:
            claims = await anyio.to_thread.run_sync(self._verifier.verify, token)
        except AuthError as exc:
            await self._send_401(send, exc.message)
            return

        forwarded = headers.get(USER_ASSERTION_HEADER, "").strip()
        user_token = forwarded or (None if _is_machine_token(claims) else token)
        if not user_token:
            logger.info(
                "no end-user assertion for this request | caller_sub=%s",
                claims.get("sub"),
            )

        bind_identity_to_scope(scope, claims, user_token)
        identity_tokens = set_identity(claims, user_token)
        try:
            await self._app(scope, receive, send)
        finally:
            reset_identity(identity_tokens)

    async def _send_401(self, send: Send, detail: str) -> None:
        metadata_url = f"{self._resource_url}/.well-known/oauth-protected-resource"
        www_auth = f'Bearer realm="{self._resource_url}", resource_metadata="{metadata_url}"'
        await _send_json(
            send,
            status=401,
            body={"error": "unauthorized", "detail": detail},
            extra_headers={"www-authenticate": www_auth},
        )


async def _send_json(
    send: Send, *, status: int, body: dict[str, str], extra_headers: dict[str, str] | None = None
) -> None:
    content = json.dumps(body).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(content)).encode("ascii")),
    ]
    for k, v in (extra_headers or {}).items():
        headers.append((k.encode("ascii"), v.encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": content, "more_body": False})


def build_protected_resource_metadata(resource_url: str, issuer: str) -> dict[str, Any]:
    """RFC 9728 document served at /.well-known/oauth-protected-resource.

    Only standard OIDC scopes are advertised: advertising the internal vanna:*
    names makes compliant clients request scopes the IdP rejects with
    invalid_scope. Read/write gating happens after verification (identity.py).
    """
    return {
        "resource": resource_url,
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid", "profile", "email", "offline_access"],
        "resource_documentation": "https://vanna.finance/docs/mcp",
    }
