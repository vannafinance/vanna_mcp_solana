"""The wallet-login step between `/authorize` and the authorization code.

`/authorize` parks the request as a *pending authorization* and sends the browser to the Vanna app's
consent page. That page is UI only: it shows who is asking, connects a wallet, and relays one message to
the wallet for signing. Every decision is made here:

  1. `challenge`  — the server builds the SIWS message (domain, wallet, client, redirect host, scopes,
                    nonce, expiry), stores it under the nonce, and returns it for the wallet to sign.
  2. `complete`   — the nonce is consumed (single use), the Ed25519 signature is verified against the
                    *server-held* message, the pending authorization is consumed (single use), and
                    only then is an authorization code minted. The browser is sent back to the client's
                    registered redirect URI with that code.

The user may narrow the scopes (read-only) but never widen them past what the client asked for.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.provider import construct_redirect_uri

from mcp_server.oauth import siws
from mcp_server.oauth.provider import (
    SCOPE_READ,
    VALID_SCOPES,
    VannaOAuthProvider,
    redirect_display,
    sanitize_client_name,
)
from mcp_server.oauth.store import hash_secret
from mcp_server.oauth.verifier import is_wallet_address

logger = logging.getLogger(__name__)

_MAX_CHALLENGES_PER_REQUEST = 5


@dataclass
class ConsentError(Exception):
    code: str
    message: str
    status: int = 400


class ConsentService:
    def __init__(self, provider: VannaOAuthProvider) -> None:
        self._provider = provider
        self._store = provider.store
        self._config = provider.config

    async def _pending(self, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not 20 <= len(request_id) <= 80:
            raise ConsentError("request_not_found", "This sign-in request is not valid.", 404)
        pending = await self._store.get(f"pending:{request_id}")
        if pending is None:
            raise ConsentError(
                "request_expired", "This sign-in request has expired or was already used. Start again from your app.", 410
            )
        return pending

    async def _client(self, pending: dict[str, Any]) -> Any:
        client = await self._provider.get_client(pending["client_id"])
        if client is None:
            raise ConsentError("request_not_found", "This sign-in request is not valid.", 404)
        return client

    async def describe(self, request_id: str) -> dict[str, Any]:
        """What the consent page shows. Contains nothing secret (no PKCE challenge, no state)."""
        pending = await self._pending(request_id)
        client = await self._client(pending)
        remaining = max(0, int(pending["expires_at"] - self._provider.now()))
        return {
            "client_name": sanitize_client_name(client.client_name),
            "redirect_host": redirect_display(pending["redirect_uri"]),
            "requested_scopes": pending["scopes"],
            "client_verified": False,
            "client_registered_at": getattr(client, "client_id_issued_at", None),
            "domain": self._config.consent_domain,
            "expires_in": remaining,
        }

    async def challenge(self, request_id: str, wallet: str, scopes: list[str] | None) -> dict[str, Any]:
        pending = await self._pending(request_id)
        if not is_wallet_address(wallet):
            raise ConsentError("invalid_wallet", "That is not a valid Solana address.")
        chosen = self._choose_scopes(pending["scopes"], scopes)
        if await self._store.incr(f"siwsn:{request_id}", self._config.pending_ttl) > _MAX_CHALLENGES_PER_REQUEST:
            raise ConsentError("too_many_attempts", "Too many sign-in attempts for this request.", 429)
        client = await self._client(pending)
        nonce = secrets.token_urlsafe(16)
        now = self._provider.now()
        message = siws.build_message(
            domain=self._config.consent_domain,
            uri=self._config.issuer,
            wallet=wallet,
            request_id=request_id,
            client_name=sanitize_client_name(client.client_name),
            client_id=client.client_id,
            redirect_host=redirect_display(pending["redirect_uri"]),
            scopes=chosen,
            nonce=nonce,
            issued_at=now,
            expires_at=now + self._config.siws_ttl,
        )
        await self._store.put(
            f"siws:{hash_secret(nonce)}",
            {"req": request_id, "wallet": wallet, "scopes": chosen, "message": message},
            self._config.siws_ttl,
        )
        return {"message": message, "nonce": nonce, "expires_in": self._config.siws_ttl}

    async def complete(self, request_id: str, wallet: str, nonce: str, signature: str) -> str:
        """Verify the wallet signature and return the redirect URL carrying the authorization code."""
        if not isinstance(nonce, str) or not isinstance(signature, str) or not is_wallet_address(wallet):
            raise ConsentError("invalid_request", "Missing or malformed sign-in fields.")
        # Single use: a replayed or concurrent completion cannot get the same challenge twice.
        challenge = await self._store.consume(f"siws:{hash_secret(nonce)}")
        if challenge is None or challenge["req"] != request_id or challenge["wallet"] != wallet:
            raise ConsentError("challenge_invalid", "The sign-in challenge expired or does not match. Try again.", 400)
        if not siws.verify_signature(wallet, challenge["message"], signature):
            logger.warning("siws signature rejected | wallet=%s", wallet)
            raise ConsentError("invalid_signature", "The wallet signature did not verify.", 401)
        # Single use: the pending authorization is consumed only after the wallet proved ownership.
        pending = await self._store.consume(f"pending:{request_id}")
        if pending is None:
            raise ConsentError(
                "request_expired", "This sign-in request has expired or was already used. Start again from your app.", 410
            )
        code = await self._provider.issue_authorization_code(
            client_id=pending["client_id"], wallet=wallet, scopes=challenge["scopes"], pending=pending
        )
        logger.info("oauth consent granted | client_id=%s wallet=%s scopes=%s", pending["client_id"], wallet, challenge["scopes"])
        return construct_redirect_uri(pending["redirect_uri"], code=code, state=pending["state"], iss=self._config.issuer)

    async def deny(self, request_id: str) -> str:
        pending = await self._store.consume(f"pending:{request_id}") if isinstance(request_id, str) else None
        if pending is None:
            raise ConsentError("request_expired", "This sign-in request has expired or was already used.", 410)
        return construct_redirect_uri(
            pending["redirect_uri"],
            error="access_denied",
            error_description="The user declined the request.",
            state=pending["state"],
            iss=self._config.issuer,
        )

    @staticmethod
    def _choose_scopes(requested: list[str], chosen: list[str] | None) -> list[str]:
        if chosen is None:
            return list(requested)
        if not isinstance(chosen, list) or not chosen or not set(chosen) <= set(requested):
            raise ConsentError("invalid_scope", "Choose a subset of the access the app asked for.")
        # Write access is meaningless without read, so it always includes it.
        merged = set(chosen) | {SCOPE_READ}
        return [s for s in VALID_SCOPES if s in merged]
