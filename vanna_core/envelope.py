"""Write-tool envelope — schema v1 (TOOLS_CATALOGUE.md §2.1, proposed in
`Vanna-Solana-Copilot-Answers.pdf` A3 and adopted).

Every write dispatcher action returns a `WriteEnvelope`. Integers are always
decimal strings (`amountRaw` etc. live in `summary`/`preview`, built by the
caller — this module only carries the transaction plumbing). The envelope
never contains a private key; `unsignedTransaction` is base64, never signed.

Field naming matches the PDF's proposed JSON shape exactly (mixed camelCase
for the transaction-plumbing fields, snake_case for the Stellar-compatible
outcome fields `signing_status`/`simulation_success`/`code`/`reason`) —
`model_dump(by_alias=True)` is how every dispatcher must serialize this.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SimulationStatus = Literal["success", "failed", "not_run_dependency"]
SigningStatus = Literal["needs_wallet_sign", "not_signable"]


class SimulationOutcome(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: SimulationStatus
    err: str | None = None
    logs: list[str] = Field(default_factory=list)
    units_consumed: int | None = Field(default=None, alias="unitsConsumed")
    context_slot: int | None = Field(default=None, alias="contextSlot")


class TransactionEnvelope(BaseModel):
    """One transaction in a write action's plan — usually exactly one; more
    than one only when a lookup-table setup round must be confirmed first
    (§2.1, Phase 3)."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    purpose: str
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    format: Literal["solana-v0"] = "solana-v0"
    encoding: Literal["base64"] = "base64"
    unsigned_transaction: str | None = Field(default=None, alias="unsignedTransaction")
    blockhash: str
    last_valid_block_height: str = Field(alias="lastValidBlockHeight")
    required_signers: list[str] = Field(default_factory=list, alias="requiredSigners")
    address_lookup_tables: list[str] = Field(default_factory=list, alias="addressLookupTables")
    simulation: SimulationOutcome


class WriteEnvelope(BaseModel):
    """The full schema-v1 envelope one write dispatcher action returns."""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal["1"] = Field(default="1", alias="schemaVersion")
    cluster: str
    program_id: str = Field(alias="programId")
    fee_payer: str = Field(alias="feePayer")
    transactions: list[TransactionEnvelope]
    summary: dict = Field(default_factory=dict)
    preview: dict | None = None
    signing_status: SigningStatus = "needs_wallet_sign"
    error: str | None = None
    code: str | None = None
    reason: str | None = None
    simulation_success: bool = False

    @classmethod
    def from_single_transaction(
        cls,
        *,
        cluster: str,
        program_id: str,
        fee_payer: str,
        purpose: str,
        unsigned_tx_b64: str,
        blockhash: str,
        last_valid_block_height: int,
        required_signers: list[str],
        simulation: SimulationOutcome,
        summary: dict,
        preview: dict | None = None,
        address_lookup_tables: list[str] | None = None,
    ) -> "WriteEnvelope":
        """The common case: one action, one transaction, nothing depends on
        anything else. Phase 3's Jupiter/lookup-table paths build the
        multi-transaction case by hand instead of this helper."""
        tx = TransactionEnvelope(
            id="action",
            purpose=purpose,
            depends_on=[],
            unsigned_transaction=unsigned_tx_b64,
            blockhash=blockhash,
            last_valid_block_height=str(last_valid_block_height),
            required_signers=required_signers,
            address_lookup_tables=address_lookup_tables or [],
            simulation=simulation,
        )
        return cls(
            cluster=cluster,
            program_id=program_id,
            fee_payer=fee_payer,
            transactions=[tx],
            summary=summary,
            preview=preview,
            simulation_success=simulation.status == "success",
            code=None if simulation.status == "success" else "simulation_failed",
            reason=simulation.err if simulation.status != "success" else None,
        )

    def to_dict(self) -> dict:
        return self.model_dump(by_alias=True)
