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

# Anchor `emit_cpi!`: an event travels as an inner instruction to the program itself, tagged with these 8 bytes.
EVENT_IX_TAG = bytes.fromhex("e445a52e51cb9a1d")
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(text: str) -> bytes:
    """Base58 (Bitcoin alphabet) to bytes: inner-instruction data in a `json`-encoded transaction."""
    n = 0
    for ch in text:
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return bytes(len(text) - len(text.lstrip("1"))) + body

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


# Read calls that are shared while in flight: two identical requests running at the same time (concurrent tool
# calls, or one read's parallel parts asking for the same account) send one request and both get its answer.
_SHARED_READS = frozenset({
    "get_account_info", "get_multiple_accounts", "get_latest_blockhash", "get_minimum_balance_for_rent_exemption",
    "get_balance", "get_token_account_balance", "get_slot", "get_block_height", "get_address_lookup_table",
})
_CLOCK_SYSVAR = "SysvarC1ock11111111111111111111111111111111"
# Answers reused for a short while after they arrive, only where that can't mislead: a recent blockhash stays valid for
# ~60 s (150 slots), the clock is read for price-age checks counted in tens of seconds, and the rent-exempt minimum is a
# constant for a given size. Account data and simulations are never reused across time: a build right after a
# confirmation always reads the new state.
# Connections to the RPC kept open between calls (see SolanaClient).
RPC_POOL_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=32, keepalive_expiry=30.0)
_BLOCKHASH_REUSE_S = 5.0
_CLOCK_REUSE_S = 1.0


def _reuse_for(name: str, args: tuple) -> float:
    if name == "get_latest_blockhash":
        return _BLOCKHASH_REUSE_S
    if name == "get_minimum_balance_for_rent_exemption":
        return float("inf")
    if name == "get_account_info" and args and str(args[0]) == _CLOCK_SYSVAR:
        return _CLOCK_REUSE_S
    return 0.0


class _ResilientRpc:
    """Proxy over `AsyncClient` that retries transient transport/gateway errors (the hosted
    fork intermittently answers 502 / "Internal error") with bounded backoff. Non-transient
    RPC errors (bad params, program errors) are raised immediately; nothing here sends a
    transaction, so retrying is always safe.

    Every tool's reads also go through here, so this is where repeats are saved for all of them: identical read calls
    in flight share one request (`_SHARED_READS`), and a few answers that can't go stale in a harmful way are reused
    briefly (`_reuse_for`). On the fork each request takes about a second, so a repeat saved is a second saved."""

    def __init__(self, inner: AsyncClient) -> None:
        self._inner = inner
        self._inflight: dict[str, asyncio.Task] = {}
        self._kept: dict[str, tuple[float, object]] = {}

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr) or name == "close":
            return attr

        async def send(*args, **kwargs):
            for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
                try:
                    return await attr(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    if delay is None or not _is_transient(exc):
                        raise
                    logger.warning("RPC %s failed transiently (%s); retry %d", name, type(exc).__name__, attempt + 1)
                    await asyncio.sleep(delay)

        if name not in _SHARED_READS:
            return send

        async def call(*args, **kwargs):
            key = f"{name}|{args!r}|{sorted(kwargs.items())!r}"
            loop = asyncio.get_running_loop()
            kept = self._kept.get(key)
            if kept is not None and loop.time() < kept[0]:
                return kept[1]
            task = self._inflight.get(key)
            if task is None:
                # Its own task: a caller that gives up (a timeout, a cancelled request) never cancels it for the others.
                task = loop.create_task(send(*args, **kwargs))
                self._inflight[key] = task

                def settled(t: asyncio.Task, key: str = key) -> None:
                    self._inflight.pop(key, None)
                    if not t.cancelled() and t.exception() is None:
                        reuse = _reuse_for(name, args)
                        if reuse > 0:
                            self._kept[key] = (loop.time() + reuse, t.result())

                task.add_done_callback(settled)
            return await asyncio.shield(task)

        return call


class SolanaClient:
    """Wraps one AsyncClient connection + the Vanna program ID."""

    def __init__(self, settings: VannaSettings) -> None:
        self._settings = settings
        self.program_id = Pubkey.from_string(settings.vanna_program_id)
        inner = AsyncClient(settings.solana_rpc_url, commitment=Confirmed)
        # Keep connections open between calls: httpx's default drops an idle one after 5 s, and a new HTTPS connection
        # to the RPC costs ~1.2-1.7 s against ~0.3 s for a request on a kept one (the fork keeps them 45 s and more).
        provider = inner._provider
        provider.session = httpx.AsyncClient(timeout=provider.session.timeout, limits=RPC_POOL_LIMITS)
        self._rpc = _ResilientRpc(inner)

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

    async def get_program_accounts(self, program_id: Pubkey, *, data_size: int, discriminator: bytes,
                                   retry: bool = True) -> list[tuple[Pubkey, bytes]]:
        """Every account of `program_id` with exactly `data_size` bytes and this 8-byte
        Anchor discriminator — used to discover the registered assets on-chain. `retry=False` makes one attempt:
        for a caller with its own fallback, the transient-error backoff only delays it."""
        from solana.rpc.types import MemcmpOpts
        rpc = self._rpc if retry else self._rpc._inner
        resp = await rpc.get_program_accounts(
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

    async def get_multiple_accounts_sliced(
        self, addresses: list[Pubkey], offset: int, length: int
    ) -> dict[str, "tuple[str, bytes] | None"]:
        """Like `get_multiple_accounts_raw`, but only bytes `[offset, offset + length)` of each account: for large
        accounts (Phoenix orderbooks are ~1.2 MB, the perp asset map ~1.6 MB) of which only a header is needed."""
        from solana.rpc.types import DataSliceOpts

        out: dict[str, tuple[str, bytes] | None] = {}
        for i in range(0, len(addresses), 100):
            chunk = addresses[i : i + 100]
            resp = await self._rpc.get_multiple_accounts(
                chunk, encoding="base64", data_slice=DataSliceOpts(offset=offset, length=length)
            )
            for address, value in zip(chunk, resp.value, strict=True):
                out[str(address)] = None if value is None else (str(value.owner), bytes(value.data))
        return out

    async def get_slot(self) -> int:
        return (await self._rpc.get_slot()).value

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

    async def get_transaction_fee_payer(self, signature) -> str | None:
        """Who paid for (and so signed) one confirmed transaction: its first account key. None if it can't be fetched."""
        resp = await self._rpc.get_transaction(signature, encoding="json", max_supported_transaction_version=0)
        if resp.value is None:
            return None
        keys = resp.value.transaction.transaction.message.account_keys
        return str(keys[0]) if keys else None

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

    async def get_transaction_event_payloads(self, signature) -> list[bytes] | None:
        """Every event payload (8-byte event discriminator + fields) one confirmed transaction emitted: Anchor log
        events (`Program data: <base64>`) and, since the 2026-10-09 upgrade, CPI events - an inner instruction from
        the program to itself whose data is the 8-byte `EVENT_IX_TAG` followed by the payload. None if the
        transaction can't be fetched."""
        import base64

        resp = await self._rpc.get_transaction(signature, encoding="json", max_supported_transaction_version=0)
        if resp.value is None or resp.value.transaction.meta is None:
            return None
        meta = resp.value.transaction.meta
        payloads: list[bytes] = []
        for log in meta.log_messages or []:
            if log.startswith("Program data: "):
                try:
                    payloads.append(base64.b64decode(log.removeprefix("Program data: ")))
                except Exception:  # noqa: BLE001
                    continue
        inner = meta.inner_instructions or []
        if inner:
            message = resp.value.transaction.transaction.message
            keys = [str(k) for k in message.account_keys]
            loaded = meta.loaded_addresses
            if loaded is not None:
                keys += [str(k) for k in loaded.writable] + [str(k) for k in loaded.readonly]
            program = str(self.program_id)
            for group in inner:
                for ix in group.instructions:
                    if ix.program_id_index >= len(keys) or keys[ix.program_id_index] != program:
                        continue
                    try:
                        data = b58decode(ix.data)
                    except Exception:  # noqa: BLE001
                        continue
                    if data[:8] == EVENT_IX_TAG:
                        payloads.append(data[8:])
        return payloads

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
        # getSignaturesForAddress, and Earn history finds deposits by the reserve address. Margin builds (swaps,
        # farm, deposit / withdraw / borrow / repay) load protocol keys; Earn transactions do not.
        if use_protocol_lookup_tables and self._settings.protocol_lookup_tables:
            from vanna_core.protocol_alt import load_protocol_tables

            given = {a.key for a in alt_accounts}
            alt_accounts += [t for t in await load_protocol_tables(self, self._settings.protocol_lookup_tables) if t.key not in given]
        wallet_alt = None
        if use_protocol_lookup_tables:
            # Since the 2026-10-09 upgrade a health-checked margin transaction names every allowed token's vault: the
            # wallet's own lookup table (`wallet_alt.py`) carries those per-wallet keys.
            from vanna_core import wallet_alt

            try:
                wallet_table = await wallet_alt.load_wallet_table(self, fee_payer)
            except wallet_alt.WalletTableNotReady as exc:
                raise ContractCallError(str(exc)) from exc
            if wallet_table is not None and wallet_table.key not in {a.key for a in alt_accounts}:
                alt_accounts.append(wallet_table)
        message = MessageV0.try_compile(fee_payer, full_instructions, alt_accounts, blockhash)
        placeholder_sigs = [Signature.default() for _ in range(message.header.num_required_signatures)]
        tx = VersionedTransaction.populate(message, placeholder_sigs)
        if wallet_alt is not None and len(bytes(tx)) > wallet_alt.MAX_TX_BYTES:
            # Too large even with the tables: the one-time setup of the wallet's table goes first (the same action is
            # built again after it lands). The margin account stays out of it (margin history is found by it).
            from vanna_core.pdas import margin_pda

            extra = wallet_alt.wanted_keys(full_instructions, fee_payer, alt_accounts, {margin_pda(fee_payer, self.program_id)[0]})
            try:
                setup = await wallet_alt.setup_envelope_if_needed(self, fee_payer, extra=extra)
            except wallet_alt.WalletTableNotReady as exc:
                raise ContractCallError(str(exc)) from exc
            if setup is not None:
                setup.summary["then"] = purpose
                return setup

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
