"""Verifies the access tokens this service itself issued.

Same checks as `mcp_server.auth.TokenVerifier` (signature, exp, iss, aud — all required) but against
the in-process key set instead of a remote JWKS, plus the structural rules of a wallet token: the
RFC 9068 `typ`, a known `kid`, a valid base58 wallet as `sub`, and a client id. A token that passes is
guaranteed to be one this service minted for exactly this resource, and `vanna_wallet` can be trusted
as the authenticated wallet.
"""

from __future__ import annotations

import logging
from typing import Any

import jwt
from solders.pubkey import Pubkey

from mcp_server.auth import AuthError
from mcp_server.identity import WALLET_CLAIM
from mcp_server.oauth.keys import ALGORITHM, TOKEN_TYPE, KeySet

logger = logging.getLogger(__name__)


def is_wallet_address(value: object) -> bool:
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        return False
    try:
        Pubkey.from_string(value)
    except ValueError:
        return False
    return True


class LocalTokenVerifier:
    def __init__(self, keys: KeySet, issuer: str, audience: str) -> None:
        self._keys = keys
        self._issuer = issuer.rstrip("/")
        self._audience = audience

    def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("Invalid token") from exc
        if header.get("alg") != ALGORITHM or header.get("typ") != TOKEN_TYPE:
            raise AuthError("Invalid token")
        public_key = self._keys.public_key_for(header.get("kid"))
        if public_key is None:
            raise AuthError("Invalid token")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                public_key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]},
                leeway=30,
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("Invalid token audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("Invalid token issuer") from exc
        except jwt.PyJWTError as exc:
            logger.warning("Wallet token validation failed: %s", type(exc).__name__)
            raise AuthError("Invalid token") from exc
        wallet = claims.get(WALLET_CLAIM)
        if not is_wallet_address(wallet) or claims.get("sub") != wallet:
            raise AuthError("Invalid token")
        if not isinstance(claims.get("client_id"), str) or not claims["client_id"]:
            raise AuthError("Invalid token")
        return claims
