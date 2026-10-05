"""Access-token signing key (ES256) and the JWKS that publishes its public half.

This key can only mint *login tokens*. It cannot move funds: the MCP never holds a wallet key, and
every transaction still needs the user's own wallet signature. Even so it is a secret — a leaked key
lets someone mint a token for any wallet — so it lives in Secret Manager, is never logged, and is
rotated by publishing the old public key at the JWKS (`OAUTH_RETIRED_PUBLIC_KEYS_PEM`) until the
tokens it signed expire (15 minutes).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

logger = logging.getLogger(__name__)

ALGORITHM = "ES256"
TOKEN_TYPE = "at+jwt"  # RFC 9068 JWT access token

_PEM_BLOCK = re.compile(r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", re.S)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _public_jwk(public_key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    numbers = public_key.public_numbers()
    x = _b64url(numbers.x.to_bytes(32, "big"))
    y = _b64url(numbers.y.to_bytes(32, "big"))
    # RFC 7638 thumbprint over the canonical member order is a stable, collision-resistant key id.
    thumbprint_input = f'{{"crv":"P-256","kty":"EC","x":"{x}","y":"{y}"}}'
    kid = _b64url(hashlib.sha256(thumbprint_input.encode("ascii")).digest())[:16]
    return {"kty": "EC", "crv": "P-256", "x": x, "y": y, "use": "sig", "alg": ALGORITHM, "kid": kid}


@dataclass
class KeySet:
    """The current signing key plus every public key that may still have live tokens."""

    _private: ec.EllipticCurvePrivateKey
    _public_keys: dict[str, ec.EllipticCurvePublicKey]
    kid: str

    @classmethod
    def from_pem(cls, private_pem: str, retired_public_pem: str = "") -> "KeySet":
        key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("OAUTH_SIGNING_KEY_PEM must be an ES256 (P-256) private key")
        return cls._build(key, retired_public_pem)

    @classmethod
    def ephemeral(cls) -> "KeySet":
        return cls._build(ec.generate_private_key(ec.SECP256R1()), "")

    @classmethod
    def _build(cls, private: ec.EllipticCurvePrivateKey, retired_public_pem: str) -> "KeySet":
        keys: dict[str, ec.EllipticCurvePublicKey] = {}
        for block in _PEM_BLOCK.findall(retired_public_pem or ""):
            public = serialization.load_pem_public_key(block.encode("utf-8"))
            if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(public.curve, ec.SECP256R1):
                raise ValueError("OAUTH_RETIRED_PUBLIC_KEYS_PEM may contain only ES256 (P-256) public keys")
            keys[_public_jwk(public)["kid"]] = public
        current = private.public_key()
        kid = _public_jwk(current)["kid"]
        keys[kid] = current
        return cls(_private=private, _public_keys=keys, kid=kid)

    @classmethod
    def from_settings(cls, settings: Any) -> "KeySet":
        if settings.oauth_signing_key_pem.strip():
            return cls.from_pem(settings.oauth_signing_key_pem, settings.oauth_retired_public_keys_pem)
        if settings.oauth_allow_ephemeral_key:
            logger.warning(
                "OAUTH_ALLOW_EPHEMERAL_KEY=true — signing with a throwaway key; every token dies when "
                "this process restarts. Never for a real deployment."
            )
            return cls.ephemeral()
        raise RuntimeError(
            "PUBLIC_MODE requires OAUTH_SIGNING_KEY_PEM (an ES256 private key from Secret Manager)."
        )

    def sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(
            claims,
            self._private,
            algorithm=ALGORITHM,
            headers={"kid": self.kid, "typ": TOKEN_TYPE},
        )

    def jwks(self) -> dict[str, Any]:
        return {"keys": [_public_jwk(pub) for pub in self._public_keys.values()]}

    def public_key_for(self, kid: str | None) -> ec.EllipticCurvePublicKey | None:
        return self._public_keys.get(kid) if kid else None
