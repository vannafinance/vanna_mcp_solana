"""Thin async Solana RPC + Anchor-program client.

No anchorpy Program/Idl — see vanna_core/anchor_codec.py for why. Instructions
are built by hand (discriminator + borsh-encoded args + explicit AccountMeta
list, taken straight from idl/vanna_lending.json's account order per
instruction); accounts are fetched raw and decoded with vanna_core/layouts.py.

Every write path returns a `WriteEnvelope` (schema v1, vanna_core/envelope.py)
built from an unsigned, simulated v0 transaction — this process holds no
private keys and never signs or submits. See TOOLS_CATALOGUE.md §2.1/§5 for
the envelope contract. The user's connected wallet signs it and the app
broadcasts; there is no Sign Service.
"""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx
from solana.exceptions import SolanaRpcException
from solana.rpc.async_api import AsyncClient
from solana.rpc.core import RPCException
from solana.rpc.commitment import Confirmed
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.compute_budget import request_heap_frame, set_compute_unit_limit

# Heap frame requested for every transaction that calls the Vanna core. The core's allocator (vanna-credit-layer
# `src/heap.rs`, 99791e1) grows upward and can use up to 256 KiB; the default 32 KiB runs out on a `margin_execute`
# into a long Jupiter route (one xStock into another) next to many position groups. Same as the app's `lib/solana/tx.ts`.
VANNA_HEAP_FRAME_BYTES = 256 * 1024


def compute_budget_prefix(instructions, program_id: Pubkey, compute_unit_limit: int | None) -> list[Instruction]:
    """The ComputeBudget instructions a build puts first: the unit limit when given, and the larger heap frame when any
    instruction calls the Vanna core. Shared by the envelope builder and the transaction-size check."""
    prefix: list[Instruction] = []
    if compute_unit_limit is not None:
        prefix.append(set_compute_unit_limit(compute_unit_limit))
    if any(ix.program_id == program_id for ix in instructions):
        prefix.append(request_heap_frame(VANNA_HEAP_FRAME_BYTES))
    return prefix
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from vanna_core.anchor_codec import check_account_discriminator
from vanna_core.config import VannaSettings
from vanna_core.envelope import SimulationOutcome, WriteEnvelope
from vanna_core.types import AccountNotFoundError, ContractCallError

logger = logging.getLogger(__name__)

_TRANSIENT_MARKERS = ("internal error", "bad gateway", "gateway", "timed out", "timeout", "temporarily", "unavailable", "502", "503", "504")
_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (SolanaRpcException, httpx.HTTPError, asyncio.TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, RPCException):
        return any(marker in str(exc).lower() for marker in _TRANSIENT_MARKERS)
    return False


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58_ALPHABET[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


class _ResilientRpc:
    """Proxy over `AsyncClient` that retries transient transport/gateway errors (the hosted
    fork intermittently answers 502 / "Internal error") with bounded backoff. Non-transient
    RPC errors (bad params, program errors) are raised immediately; nothing here sends a
    transaction, so retrying is always safe."""

    def __init__(self, inner: AsyncClient) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr) or name == "close":
            return attr

        async def call(*args, **kwargs):
            for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
                try:
                    return await attr(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    if delay is None or not _is_transient(exc):
                        raise
                    logger.warning("RPC %s failed transiently (%s); retry %d", name, type(exc).__name__, attempt + 1)
                    await asyncio.sleep(delay)

        return call


class SolanaClient:
    """Wraps one AsyncClient connection + the Vanna program ID."""

    def __init__(self, settings: VannaSettings) -> None:
        self._settings = settings
        self.program_id = Pubkey.from_string(settings.vanna_program_id)
        self._rpc = _ResilientRpc(AsyncClient(settings.solana_rpc_url, commitment=Confirmed))

    @property
    def settings(self) -> VannaSettings:
        return self._settings

    async def close(self) -> None:
        await self._rpc._inner.close()

    # -- reads ----------------------------------------------------------------

    async def fetch_raw_account(self, address: Pubkey) -> bytes:
        """Fetch an account's raw data, owned-by-program not enforced here."""
        resp = await self._rpc.get_account_info(address, encoding="base64")
        if resp.value is None:
            raise AccountNotFoundError(f"Account not found: {address}")
        return bytes(resp.value.data)

    async def fetch_raw_account_or_none(self, address: Pubkey) -> bytes | None:
        try:
            return await self.fetch_raw_account(address)
        except AccountNotFoundError:
            return None

    async def fetch_account_with_lamports_or_none(self, address: Pubkey) -> tuple[int, bytes] | None:
        resp = await self._rpc.get_account_info(address, encoding="base64")
        if resp.value is None:
            return None
        return resp.value.lamports, bytes(resp.value.data)

    async def fetch_program_account(self, address: Pubkey, struct_name: str, layout) -> object:
        """Fetch + verify-owner + discriminator-check + borsh-decode one account.

        Raises:
            AccountNotFoundError: The account does not exist.
            ContractCallError: The account exists but isn't owned by this
                program, or its discriminator doesn't match ``struct_name``.
        """
        resp = await self._rpc.get_account_info(address, encoding="base64")
        if resp.value is None:
            raise AccountNotFoundError(f"Account not found: {address}")
        if resp.value.owner != self.program_id:
            raise ContractCallError(
                f"Account {address} is not owned by the Vanna program "
                f"(owner={resp.value.owner})"
            )
        data = bytes(resp.value.data)
        try:
            payload = check_account_discriminator(data, struct_name)
            return layout.parse(payload)
        except Exception as exc:  # noqa: BLE001
            raise ContractCallError(
                f"Failed to decode {struct_name} at {address}: {exc}"
            ) from exc

    async def fetch_program_account_or_none(
        self, address: Pubkey, struct_name: str, layout
    ) -> object | None:
        try:
            return await self.fetch_program_account(address, struct_name, layout)
        except AccountNotFoundError:
            return None

    async def fetch_raw_account_with_owner(self, address: Pubkey, owner_program_id: Pubkey) -> bytes:
        """Like `fetch_raw_account`, but also verifies the account's owner —
        for foreign-program accounts this module doesn't have a borsh layout
        for (e.g. Kamino's `Reserve`, read by manual byte offsets)."""
        resp = await self._rpc.get_account_info(address, encoding="base64")
        if resp.value is None:
            raise AccountNotFoundError(f"Account not found: {address}")
        if resp.value.owner != owner_program_id:
            raise ContractCallError(f"Account {address} is not owned by {owner_program_id} (owner={resp.value.owner})")
        return bytes(resp.value.data)

    async def fetch_foreign_program_account(
        self, address: Pubkey, owner_program_id: Pubkey, struct_name: str, layout
    ) -> object:
        """Like `fetch_program_account`, but for an account owned by a
        program other than the Vanna program (e.g. a Pyth `PriceUpdateV2`,
        owned by the Pyth Receiver program)."""
        resp = await self._rpc.get_account_info(address, encoding="base64")
        if resp.value is None:
            raise AccountNotFoundError(f"Account not found: {address}")
        if resp.value.owner != owner_program_id:
            raise ContractCallError(
                f"Account {address} is not owned by {owner_program_id} (owner={resp.value.owner})"
            )
        data = bytes(resp.value.data)
        try:
            payload = check_account_discriminator(data, struct_name)
            return layout.parse(payload)
        except Exception as exc:  # noqa: BLE001
            raise ContractCallError(
                f"Failed to decode {struct_name} at {address}: {exc}"
            ) from exc

    async def get_program_accounts(self, program_id: Pubkey, *, data_size: int, discriminator: bytes) -> list[tuple[Pubkey, bytes]]:
        """Every account of `program_id` with exactly `data_size` bytes and this 8-byte
        Anchor discriminator — used to discover the registered assets on-chain."""
        from solana.rpc.types import MemcmpOpts
        resp = await self._rpc.get_program_accounts(
            program_id, encoding="base64",
            filters=[data_size, MemcmpOpts(offset=0, bytes=_b58encode(discriminator))],
        )
        return [(item.pubkey, bytes(item.account.data)) for item in resp.value]

    async def get_multiple_accounts_raw(self, addresses: list[Pubkey]) -> dict[str, "tuple[str, bytes] | None"]:
        """{address: (owner, data)} for every address (None if it doesn't exist), one batched
        RPC call per 100 addresses."""
        out: dict[str, tuple[str, bytes] | None] = {}
        for i in range(0, len(addresses), 100):
            chunk = addresses[i : i + 100]
            resp = await self._rpc.get_multiple_accounts(chunk, encoding="base64")
            for address, value in zip(chunk, resp.value, strict=True):
                out[str(address)] = None if value is None else (str(value.owner), bytes(value.data))
        return out

    async def account_exists(self, address: Pubkey) -> bool:
        resp = await self._rpc.get_account_info(address, encoding="base64")
        return resp.value is not None

    async def get_sol_balance(self, address: Pubkey) -> int:
        resp = await self._rpc.get_balance(address)
        return resp.value

    async def get_latest_blockhash(self):
        resp = await self._rpc.get_latest_blockhash()
        return resp.value.blockhash, resp.value.last_valid_block_height

    async def simulate_versioned_transaction(self, tx: VersionedTransaction):
        return await self._rpc.simulate_transaction(tx, sig_verify=False)

    async def get_signature_status(self, signature):
        resp = await self._rpc.get_signature_statuses([signature])
        return resp.value[0]

    async def get_signatures_for_address(self, address: Pubkey, limit: int = 50):
        resp = await self._rpc.get_signatures_for_address(address, limit=limit)
        return resp.value

    async def get_transaction_logs(self, signature) -> list[str] | None:
        """The log messages for one confirmed transaction, or None if it
        can't be fetched/decoded (never batched — some RPC nodes, including
        the hosted fork, reject batched JSON-RPC)."""
        resp = await self._rpc.get_transaction(
            signature, encoding="json", max_supported_transaction_version=0
        )
        if resp.value is None or resp.value.transaction.meta is None:
            return None
        return resp.value.transaction.meta.log_messages or []

    async def get_chain_clock_unix_timestamp(self) -> int:
        """Read the Clock sysvar's unix_timestamp — the chain-time basis
        every price-age and interest-accrual check uses (never wall-clock
        time). Layout: slot(u64) epoch_start_timestamp(i64) epoch(u64)
        leader_schedule_epoch(u64) unix_timestamp(i64), offset 32."""
        clock_sysvar = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")
        data = await self.fetch_raw_account(clock_sysvar)
        return int.from_bytes(data[32:40], "little", signed=True)

    # -- writes -----------------------------------------------------------------

    def build_instruction(
        self, discriminator: bytes, args_bytes: bytes, accounts: list[AccountMeta]
    ) -> Instruction:
        return Instruction(
            program_id=self.program_id,
            data=discriminator + args_bytes,
            accounts=accounts,
        )

    async def build_write_envelope(
        self,
        instructions: list[Instruction],
        fee_payer: Pubkey,
        purpose: str,
        summary: dict,
        *,
        compute_unit_limit: int | None = None,
        required_signers: list[Pubkey] | None = None,
        address_lookup_tables: list[AddressLookupTableAccount] | None = None,
        preview: dict | None = None,
        use_protocol_lookup_tables: bool = False,
    ) -> WriteEnvelope:
        """Build, compile as v0, simulate (sig_verify disabled — nothing is
        signed yet), and wrap the result in a schema-v1 WriteEnvelope. Never
        signs or submits.

        `compute_unit_limit` prepends a `ComputeBudget.setComputeUnitLimit`
        instruction (TOOLS_CATALOGUE.md §2.2: 600k for batched instructions,
        1.4M for `margin_execute`) — omit it to let the cluster's default
        apply for a single simple instruction.
        """
        full_instructions = [*compute_budget_prefix(instructions, self.program_id, compute_unit_limit), *instructions]

        blockhash_resp = await self._rpc.get_latest_blockhash()
        blockhash = blockhash_resp.value.blockhash
        last_valid_block_height = blockhash_resp.value.last_valid_block_height

        alt_accounts = list(address_lookup_tables or [])
        # Opt-in per build, not global: an account loaded from a lookup table is not indexed by the fork's
        # getSignaturesForAddress, and Earn history finds deposits by the reserve address. Only builds that
        # need the room (margin swaps) load protocol keys; every other transaction is unchanged.
        if use_protocol_lookup_tables and self._settings.protocol_lookup_tables:
            from vanna_core.protocol_alt import load_protocol_tables

            given = {a.key for a in alt_accounts}
            alt_accounts += [t for t in await load_protocol_tables(self, self._settings.protocol_lookup_tables) if t.key not in given]
        message = MessageV0.try_compile(fee_payer, full_instructions, alt_accounts, blockhash)
        placeholder_sigs = [Signature.default() for _ in range(message.header.num_required_signatures)]
        tx = VersionedTransaction.populate(message, placeholder_sigs)

        sim = await self._rpc.simulate_transaction(tx, sig_verify=False)
        success = sim.value.err is None
        error_str = None
        if not success:
            logs_tail = "\n".join((sim.value.logs or [])[-6:])
            error_str = f"{sim.value.err}\n{logs_tail}"
            logger.warning("Simulation failed for %s: %s", purpose, error_str)

        simulation = SimulationOutcome(
            status="success" if success else "failed",
            err=error_str,
            logs=list(sim.value.logs or []),
            units_consumed=sim.value.units_consumed,
            context_slot=sim.context.slot if sim.context else None,
        )

        signers = required_signers if required_signers is not None else [fee_payer]
        return WriteEnvelope.from_single_transaction(
            cluster=self._settings.solana_cluster_label,
            program_id=str(self.program_id),
            fee_payer=str(fee_payer),
            purpose=purpose,
            unsigned_tx_b64=base64.b64encode(bytes(tx)).decode(),
            blockhash=str(blockhash),
            last_valid_block_height=last_valid_block_height,
            required_signers=[str(s) for s in signers],
            simulation=simulation,
            summary=summary,
            preview=preview,
            address_lookup_tables=[str(a.key) for a in alt_accounts],
        )


def compute_idl_sha256(idl_path) -> str:
    """SHA-256 of the IDL file with line endings normalised to LF, so a CRLF
    checkout hashes the same as the pinned LF form."""
    import hashlib
    import pathlib

    data = pathlib.Path(idl_path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()
