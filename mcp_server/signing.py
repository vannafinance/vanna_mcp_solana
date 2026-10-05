"""Signature plumbing for the plan engine: verify what the user's wallet signed, relay it, read its status.

The MCP client (Claude, Cursor) cannot sign and must never hold a key. A user signs in their own wallet in a browser
page this server hosts; this module is the part that decides whether what comes back is acceptable:

  * the signed message must be byte-identical to the transaction the server just built (a wallet or page that altered
    anything is refused — the client never supplies amounts);
  * the only required signer must be the plan's wallet, and its Ed25519 signature must verify.

The server never signs anything and holds no key. Relaying a transaction the user's wallet already signed is the same
step the app's own server performs for the website and the Copilot.
"""

from __future__ import annotations

import hashlib
from typing import Any, Awaitable, Callable

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from solders.message import to_bytes_versioned
from solders.signature import Signature
from solders.transaction import VersionedTransaction

Sender = Callable[[str], Awaitable[str]]  # signed tx base64 -> signature (raises SendError)
StatusReader = Callable[[list[str]], Awaitable[list[dict[str, Any] | None]]]

MAX_SIGNED_TX_BYTES = 2048


class SendError(Exception):
    """The network explicitly rejected the transaction (it did not land)."""


def message_hash(tx: VersionedTransaction) -> str:
    return hashlib.sha256(to_bytes_versioned(tx.message)).hexdigest()


def fee_payer(tx: VersionedTransaction) -> str:
    return str(list(tx.message.account_keys)[0])


def verify_signed_transaction(tx: VersionedTransaction, wallet: str) -> str | None:
    """Reason the signed transaction is unacceptable, or None. The only required signer (the fee payer) must be
    `wallet`, and its signature must verify over the message."""
    message = tx.message
    required = message.header.num_required_signatures
    keys = list(message.account_keys)[:required]
    if required != 1 or not keys or str(keys[0]) != wallet:
        return "The transaction must be paid for and signed by your connected wallet only."
    signatures = list(tx.signatures)
    if len(signatures) != required:
        return "The transaction is missing a signature."
    signed_bytes = to_bytes_versioned(message)
    for key, signature in zip(keys, signatures):
        if signature == Signature.default():
            return "The transaction is not signed."
        try:
            Ed25519PublicKey.from_public_bytes(bytes(key)).verify(bytes(signature), signed_bytes)
        except InvalidSignature:
            return "The wallet signature does not verify."
    return None


def rpc_sender(rpc_url: str) -> Sender:
    """sendTransaction through raw JSON-RPC (solana-py's parser crashes on the fork's preflight-failure body)."""

    async def send(signed_b64: str) -> str:
        async with httpx.AsyncClient(timeout=30.0) as http:
            for attempt in range(3):
                resp = await http.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                        "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}],
                    },
                )
                body = resp.json()
                if "result" in body:
                    return str(body["result"])
                message = str((body.get("error") or {}).get("message") or body.get("error"))
                if "blockhash not found" in message.lower() and attempt < 2:
                    continue
                raise SendError(message)
        raise SendError("sendTransaction failed")

    return send


def rpc_status_reader(rpc_url: str) -> StatusReader:
    async def read(signatures: list[str]) -> list[dict[str, Any] | None]:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                      "params": [signatures, {"searchTransactionHistory": True}]},
            )
        return list((resp.json().get("result") or {}).get("value") or [None] * len(signatures))

    return read
