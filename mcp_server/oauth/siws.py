"""Sign-In-With-Solana for the connector login.

The server builds the message, stores it, and later verifies the wallet's Ed25519 signature over those
exact bytes. The browser only relays the message to the wallet and the signature back, so it cannot
change what is being authorized. The message binds everything that must not be replayable elsewhere:
the page domain the wallet shows, the wallet, the pending authorization, the client and where its
redirect goes, the scopes the user chose, a single-use nonce and a short expiry.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from solders.pubkey import Pubkey

SIGNATURE_BYTES = 64


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_message(
    *,
    domain: str,
    uri: str,
    wallet: str,
    request_id: str,
    client_name: str,
    client_id: str,
    redirect_host: str,
    scopes: list[str],
    nonce: str,
    issued_at: float,
    expires_at: float,
) -> str:
    access = "read and build transactions for" if "vanna:write" in scopes else "read data for"
    return (
        f"{domain} wants you to sign in with your Solana account:\n"
        f"{wallet}\n"
        f"\n"
        f'Allow "{client_name}" ({redirect_host}) to {access} this wallet on Vanna Finance. '
        f"Signing this message does not move funds; every transaction still needs your approval in your wallet.\n"
        f"\n"
        f"URI: {uri}\n"
        f"Version: 1\n"
        f"Client ID: {client_id}\n"
        f"Scopes: {' '.join(scopes)}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {_iso(issued_at)}\n"
        f"Expiration Time: {_iso(expires_at)}\n"
        f"Request ID: {request_id}"
    )


def verify_signature(wallet: str, message: str, signature_b64: str) -> bool:
    """True only if `signature_b64` is a valid Ed25519 signature of `message` by `wallet`."""
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        if len(signature) != SIGNATURE_BYTES:
            return False
        public_key = Ed25519PublicKey.from_public_bytes(bytes(Pubkey.from_string(wallet)))
        public_key.verify(signature, message.encode("utf-8"))
    except (binascii.Error, ValueError, InvalidSignature):
        return False
    return True
