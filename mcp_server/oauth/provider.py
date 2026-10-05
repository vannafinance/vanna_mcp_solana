"""The connector's OAuth 2.1 authorization server (wallet-as-login).

Implements the MCP SDK's `OAuthAuthorizationServerProvider`: the SDK handlers own protocol validation
(PKCE S256, redirect_uri match, scope syntax, client authentication); this class owns the decisions —
who may register, what a pending authorization is, how a wallet proves ownership, and what a token
means. The login itself happens in `consent.py`: the user's wallet signs a SIWS message the server
built, and only then is an authorization code minted.

Security properties (see MCP_CONNECTOR_PLAN.md §6.1, §9):
  * Dynamic registration accepts only https / loopback / private-use-scheme redirect URIs, exactly matched.
  * An authorization code is single-use, lives 60 s, is stored hashed, and replaying it revokes the
    tokens it produced (RFC 6749 §4.1.2).
  * Refresh tokens rotate on every use; presenting an already-used one revokes the whole family.
  * Access tokens are 15-minute ES256 JWTs bound to this resource (`aud`) and carrying the wallet.
  * Nothing is ever issued without a verified wallet signature.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_server.oauth.keys import KeySet
from mcp_server.oauth.store import RecordStore, hash_secret
from mcp_server.identity import WALLET_CLAIM

logger = logging.getLogger(__name__)

SCOPE_READ = "vanna:read"
SCOPE_WRITE = "vanna:write"
VALID_SCOPES = (SCOPE_READ, SCOPE_WRITE)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_PRIVATE_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]{1,31}$")
# Schemes that must never be a redirect target (script execution, local files, plain web sockets...).
_DENIED_SCHEMES = frozenset(
    {"javascript", "data", "file", "vbscript", "about", "blob", "ftp", "ftps", "ws", "wss", "chrome", "view-source"}
)
_MAX_REDIRECT_URIS = 5
_CLIENT_TTL_SECONDS = 180 * 86400
_MAX_CLIENT_NAME = 80
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f  ]")


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str  # the public base URL; no trailing slash
    consent_url: str
    access_ttl: int = 900
    refresh_ttl: int = 30 * 86400
    code_ttl: int = 60
    pending_ttl: int = 600
    siws_ttl: int = 120
    dcr_per_ip_per_hour: int = 10
    dcr_global_per_day: int = 2000

    @property
    def resource(self) -> str:
        return f"{self.issuer}/mcp"

    @property
    def consent_origin(self) -> str:
        parsed = urlparse(self.consent_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def consent_domain(self) -> str:
        return urlparse(self.consent_url).netloc

    @classmethod
    def from_settings(cls, settings: Any) -> "OAuthConfig":
        issuer = settings.external_base_url.rstrip("/")
        if not issuer:
            raise RuntimeError("PUBLIC_MODE requires EXTERNAL_BASE_URL (the public https URL of this service).")
        if not _is_secure_or_loopback(issuer):
            raise RuntimeError("EXTERNAL_BASE_URL must be https (http is allowed only for localhost).")
        # Defaults to this service's own /connect page (mcp_server/web), so no separate app page is needed.
        consent = settings.oauth_consent_url.strip() or f"{issuer}/connect"
        if not _is_secure_or_loopback(consent):
            raise RuntimeError("OAUTH_CONSENT_URL must be https (http is allowed only for localhost).")
        return cls(
            issuer=issuer,
            consent_url=consent,
            access_ttl=settings.oauth_access_token_ttl_seconds,
            refresh_ttl=settings.oauth_refresh_token_ttl_seconds,
            code_ttl=settings.oauth_code_ttl_seconds,
            pending_ttl=settings.oauth_pending_ttl_seconds,
            siws_ttl=settings.oauth_siws_ttl_seconds,
            dcr_per_ip_per_hour=settings.oauth_dcr_per_ip_per_hour,
            dcr_global_per_day=settings.oauth_dcr_global_per_day,
        )


def _is_secure_or_loopback(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    return parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS


def sanitize_client_name(name: str | None) -> str:
    """Attacker-controlled text shown on the consent page and inside the signed message."""
    cleaned = _CONTROL_CHARS.sub(" ", name or "").replace('"', "'").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:_MAX_CLIENT_NAME]
    return cleaned or "Unnamed MCP client"


def redirect_display(uri: str) -> str:
    """The host a user is actually authorizing, for the consent page and the signed message."""
    parsed = urlparse(uri)
    if parsed.scheme in ("http", "https"):
        return parsed.netloc
    return f"{parsed.scheme}://{parsed.netloc or parsed.path.lstrip('/').split('/')[0]}"


def validate_redirect_uri(uri: str) -> str | None:
    """Reason a redirect URI is not acceptable, or None. Exact match is enforced later by the SDK."""
    if len(uri) > 512 or _CONTROL_CHARS.search(uri) or "*" in uri:
        return "redirect_uri is malformed"
    parsed = urlparse(uri)
    if parsed.fragment:
        return "redirect_uri must not contain a fragment"
    if parsed.username is not None or parsed.password is not None:
        return "redirect_uri must not contain credentials"
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return None if parsed.hostname else "redirect_uri needs a host"
    if scheme == "http":
        if parsed.hostname in _LOOPBACK_HOSTS:
            return None
        return "http redirect_uri is only allowed for localhost"
    if scheme in _DENIED_SCHEMES or not _PRIVATE_SCHEME.match(scheme):
        return f"redirect_uri scheme {scheme!r} is not allowed"
    return None


def _new_secret(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)  # 256 bits


class VannaAuthorizationCode(AuthorizationCode):
    family: str
    wallet: str


class VannaRefreshToken(RefreshToken):
    family: str
    wallet: str
    absolute_expires_at: int


class VannaAccessToken(AccessToken):
    family: str | None = None


class VannaOAuthProvider:
    """`OAuthAuthorizationServerProvider` backed by a `RecordStore` and an ES256 key set."""

    def __init__(
        self,
        store: RecordStore,
        keys: KeySet,
        config: OAuthConfig,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.keys = keys
        self.config = config
        self._clock = clock

    def now(self) -> float:
        return self._clock()

    # ------------------------------------------------------------------ clients (RFC 7591)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        record = await self.store.get(f"client:{client_id}")
        if record is None:
            return None
        return OAuthClientInformationFull.model_validate(record)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or len(uris) > _MAX_REDIRECT_URIS:
            raise RegistrationError("invalid_redirect_uri", f"Provide between 1 and {_MAX_REDIRECT_URIS} redirect URIs.")
        for uri in uris:
            reason = validate_redirect_uri(uri)
            if reason:
                raise RegistrationError("invalid_redirect_uri", reason)
        if set(client_info.grant_types) - {"authorization_code", "refresh_token"}:
            raise RegistrationError("invalid_client_metadata", "Only authorization_code and refresh_token grants are supported.")
        if client_info.response_types != ["code"]:
            raise RegistrationError("invalid_client_metadata", "Only the 'code' response type is supported.")
        if client_info.scope:
            requested = set(client_info.scope.split())
            if not requested <= set(VALID_SCOPES):
                raise RegistrationError("invalid_client_metadata", f"Supported scopes: {' '.join(VALID_SCOPES)}.")
        else:
            client_info.scope = " ".join(VALID_SCOPES)
        # Registration is open by design (MCP clients self-register), so it is rate limited and capped.
        day = int(self._clock() // 86400)
        if await self.store.incr(f"dcr:day:{day}", 86400) > self.config.dcr_global_per_day:
            raise RegistrationError("invalid_client_metadata", "Registration is temporarily unavailable. Try again later.")
        client_info.client_name = sanitize_client_name(client_info.client_name)
        await self.store.put(f"client:{client_info.client_id}", client_info.model_dump(mode="json"), _CLIENT_TTL_SECONDS)
        logger.info("oauth client registered | client_id=%s redirect_hosts=%s", client_info.client_id,
                    sorted({redirect_display(u) for u in uris}))

    # ------------------------------------------------------------------ authorize

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Create a pending authorization and send the browser to the consent page. Nothing is granted yet."""
        if params.resource is not None and params.resource.rstrip("/") != self.config.resource:
            raise AuthorizeError("invalid_target", "resource must be this MCP server's URL.")
        # No scope in the request means "whatever this client registered for" (never more).
        requested = params.scopes or (client.scope or " ".join(VALID_SCOPES)).split()
        if not set(requested) <= set(VALID_SCOPES):
            raise AuthorizeError("invalid_scope", f"Supported scopes: {' '.join(VALID_SCOPES)}.")
        request_id = _new_secret()
        now = self._clock()
        await self.store.put(
            f"pending:{request_id}",
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": sorted(set(requested), key=VALID_SCOPES.index),
                "created_at": now,
                "expires_at": now + self.config.pending_ttl,
            },
            self.config.pending_ttl,
        )
        separator = "&" if "?" in self.config.consent_url else "?"
        return f"{self.config.consent_url}{separator}{urlencode({'req': request_id})}"

    # ------------------------------------------------------------------ authorization codes

    async def issue_authorization_code(
        self, *, client_id: str, wallet: str, scopes: list[str], pending: dict[str, Any]
    ) -> str:
        """Mint the code for a wallet that just proved ownership. Called only by consent.complete()."""
        code = _new_secret()
        now = self._clock()
        record = VannaAuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=now + self.config.code_ttl,
            client_id=client_id,
            code_challenge=pending["code_challenge"],
            redirect_uri=pending["redirect_uri"],
            redirect_uri_provided_explicitly=pending["redirect_uri_provided_explicitly"],
            resource=self.config.resource,
            subject=wallet,
            family=uuid.uuid4().hex,
            wallet=wallet,
        )
        stored = record.model_dump(mode="json")
        stored.pop("code")  # the key is its hash; the secret itself is never at rest
        await self.store.put(f"code:{hash_secret(code)}", stored, self.config.code_ttl)
        return code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> VannaAuthorizationCode | None:
        key = hash_secret(authorization_code)
        record = await self.store.get(f"code:{key}")
        if record is None:
            used = await self.store.get(f"codeused:{key}")
            if used is not None:
                # An already-redeemed code came back: assume it was intercepted and kill what it produced.
                await self._revoke_family(used["family"])
                logger.warning("authorization code replayed | client_id=%s family=%s", client.client_id, used["family"])
            return None
        if record["client_id"] != client.client_id:
            return None
        return VannaAuthorizationCode.model_validate({**record, "code": authorization_code})

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: VannaAuthorizationCode
    ) -> OAuthToken:
        key = hash_secret(authorization_code.code)
        # Single use: only one concurrent redeemer gets the record.
        if await self.store.consume(f"code:{key}") is None:
            # Lost a race with another redeemer of the same code: assume interception, kill the grant.
            await self._revoke_family(authorization_code.family)
            raise TokenError("invalid_grant", "authorization code does not exist")
        await self.store.put(f"codeused:{key}", {"family": authorization_code.family}, self.config.refresh_ttl)
        absolute = int(self._clock()) + self.config.refresh_ttl
        return await self._issue_tokens(
            wallet=authorization_code.wallet,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            family=authorization_code.family,
            absolute_expires_at=absolute,
        )

    # ------------------------------------------------------------------ refresh tokens

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> VannaRefreshToken | None:
        key = hash_secret(refresh_token)
        record = await self.store.get(f"rt:{key}")
        if record is None:
            used = await self.store.get(f"rtused:{key}")
            if used is not None:
                await self._revoke_family(used["family"])
                logger.warning("refresh token reuse detected | client_id=%s family=%s", client.client_id, used["family"])
            return None
        if record["client_id"] != client.client_id or await self._is_revoked(record["family"]):
            return None
        return VannaRefreshToken.model_validate({**record, "token": refresh_token})

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: VannaRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        key = hash_secret(refresh_token.token)
        if await self.store.consume(f"rt:{key}") is None:
            # Lost a race with another redeemer of the same token: treat as theft.
            await self._revoke_family(refresh_token.family)
            raise TokenError("invalid_grant", "refresh token does not exist")
        remaining = refresh_token.absolute_expires_at - int(self._clock())
        await self.store.put(f"rtused:{key}", {"family": refresh_token.family}, max(remaining, 1))
        if remaining <= 0:
            raise TokenError("invalid_grant", "refresh token has expired")
        return await self._issue_tokens(
            wallet=refresh_token.wallet,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            family=refresh_token.family,
            absolute_expires_at=refresh_token.absolute_expires_at,
        )

    # ------------------------------------------------------------------ access tokens

    async def load_access_token(self, token: str) -> VannaAccessToken | None:
        # Request authentication runs through AuthMiddleware + LocalTokenVerifier; this exists for the
        # SDK's revocation handler, which must be able to resolve an access token to its family.
        from mcp_server.auth import AuthError
        from mcp_server.oauth.verifier import LocalTokenVerifier

        try:
            claims = LocalTokenVerifier(self.keys, self.config.issuer, self.config.resource).verify(token)
        except AuthError:
            return None
        return VannaAccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=self.config.resource,
            subject=claims["sub"],
            claims=claims,
            family=claims.get("fam"),
        )

    async def revoke_token(self, token: VannaAccessToken | VannaRefreshToken) -> None:
        family = token.family
        if family:
            await self._revoke_family(family)

    async def is_token_active(self, claims: dict[str, Any]) -> bool:
        """False once the grant a token belongs to has been revoked (checked on every request)."""
        family = claims.get("fam")
        return isinstance(family, str) and not await self._is_revoked(family)

    # ------------------------------------------------------------------ internals

    async def _issue_tokens(
        self, *, wallet: str, client_id: str, scopes: list[str], family: str, absolute_expires_at: int
    ) -> OAuthToken:
        now = int(self._clock())
        scope = " ".join(scopes)
        access = self.keys.sign(
            {
                "iss": self.config.issuer,
                "aud": self.config.resource,
                "sub": wallet,
                WALLET_CLAIM: wallet,
                "client_id": client_id,
                "scope": scope,
                "fam": family,
                "jti": uuid.uuid4().hex,
                "iat": now,
                "nbf": now,
                "exp": now + self.config.access_ttl,
            }
        )
        refresh = _new_secret("vrt_")
        await self.store.put(
            f"rt:{hash_secret(refresh)}",
            VannaRefreshToken(
                token="-",
                client_id=client_id,
                scopes=scopes,
                expires_at=absolute_expires_at,
                resource=self.config.resource,
                subject=wallet,
                family=family,
                wallet=wallet,
                absolute_expires_at=absolute_expires_at,
            ).model_dump(mode="json", exclude={"token"}),
            max(absolute_expires_at - now, 1),
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.config.access_ttl,
            scope=scope,
            refresh_token=refresh,
        )

    async def _revoke_family(self, family: str) -> None:
        await self.store.put(f"revoked:{family}", {"at": self._clock()}, self.config.refresh_ttl)

    async def _is_revoked(self, family: str) -> bool:
        return await self.store.get(f"revoked:{family}") is not None
