"""Manual Anchor instruction/account discriminator + encoding helpers.

Why this exists instead of anchorpy's Idl/Program: anchorpy 0.21.0's IDL parser
(anchorpy-core, a Rust extension) cannot parse the vanna_lending IDL — it uses
the newer per-instruction/per-account explicit "discriminator" array format
that shipped with Anchor 0.29+/0.30+ (`idl.instructions[i].discriminator`,
`idl.accounts[i].discriminator`), and anchorpy-core's untagged-enum decoder
rejects it with:

    SerdeJSONError: data did not match any variant of untagged enum
    IdlAccountItem

(Reproduced and confirmed against idl/vanna_lending.json on 2026-09-28 — no
compatible anchorpy release was available to install.)

Anchor's discriminators are *derived*, not arbitrary — this module recomputes
them from the instruction/account name instead of depending on any IDL parser:

    instruction discriminator = sha256(f"global:{snake_case_name}")[:8]
    account discriminator     = sha256(f"account:{StructName}")[:8]

Cross-checked byte-for-byte against every discriminator declared in
idl/vanna_lending.json (see the repo's test suite) — they match exactly, so
this is exactly what the deployed Anchor program itself expects, not a
best-effort reimplementation.
"""

from __future__ import annotations

import hashlib


def instruction_discriminator(ix_name: str) -> bytes:
    """8-byte Anchor instruction discriminator for a snake_case instruction name."""
    return hashlib.sha256(f"global:{ix_name}".encode()).digest()[:8]


def account_discriminator(struct_name: str) -> bytes:
    """8-byte Anchor account discriminator for a PascalCase account struct name."""
    return hashlib.sha256(f"account:{struct_name}".encode()).digest()[:8]


def check_account_discriminator(data: bytes, struct_name: str) -> bytes:
    """Validate an account's leading 8-byte discriminator and return the remaining payload.

    Raises:
        ValueError: If ``data`` is too short or the discriminator doesn't match.
    """
    expected = account_discriminator(struct_name)
    if len(data) < 8:
        raise ValueError(f"Account data too short to hold a discriminator: {len(data)} bytes")
    if data[:8] != expected:
        raise ValueError(
            f"Account discriminator mismatch for {struct_name}: "
            f"expected {expected.hex()}, got {data[:8].hex()}"
        )
    return data[8:]
