"""Thin async Solana RPC + Anchor-program client.

No anchorpy Program/Idl — see vanna_core/anchor_codec.py for why. Instructions
are built by hand (discriminator + borsh-encoded args + explicit AccountMeta
list, taken straight from idl/vanna_lending.json's account order per
instruction); accounts are fetched raw and decoded with vanna_core/layouts.py.

Every write path returns an UnsignedTransaction (simulated, never signed or
submitted) — this process holds no private keys. See
VANNA_SOLANA_MCP_REQUIREMENTS.md Part A #3 for the sign-service handoff this
gets forwarded to.
"""

from __future__ import annotations

import base64
import logging

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from vanna_core.anchor_codec import check_account_discriminator
from vanna_core.config import VannaSettings
from vanna_core.types import AccountNotFoundError, ContractCallError, UnsignedTransaction

logger = logging.getLogger(__name__)


class SolanaClient:
    """Wraps one AsyncClient connection + the Vanna program ID."""

    def __init__(self, settings: VannaSettings) -> None:
        self._settings = settings
        self.program_id = Pubkey.from_string(settings.vanna_program_id)
        self._rpc = AsyncClient(settings.solana_rpc_url, commitment=Confirmed)

    async def close(self) -> None:
        await self._rpc.close()

    # -- reads ----------------------------------------------------------------

    async def fetch_raw_account(self, address: Pubkey) -> bytes:
        """Fetch an account's raw data, owned-by-program not enforced here."""
        resp = await self._rpc.get_account_info(address, encoding="base64")
        if resp.value is None:
            raise AccountNotFoundError(f"Account not found: {address}")
        return bytes(resp.value.data)

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

    async def account_exists(self, address: Pubkey) -> bool:
        resp = await self._rpc.get_account_info(address, encoding="base64")
        return resp.value is not None

    # -- writes -----------------------------------------------------------------

    def build_instruction(
        self, discriminator: bytes, args_bytes: bytes, accounts: list[AccountMeta]
    ) -> Instruction:
        return Instruction(
            program_id=self.program_id,
            data=discriminator + args_bytes,
            accounts=accounts,
        )

    async def simulate_and_build_unsigned(
        self,
        instructions: list[Instruction],
        fee_payer: Pubkey,
        instruction_name: str,
    ) -> UnsignedTransaction:
        """Build, simulate (sig_verify disabled — nothing is signed yet), and
        return an UnsignedTransaction. Never signs or submits.
        """
        blockhash_resp = await self._rpc.get_latest_blockhash()
        message = Message.new_with_blockhash(
            instructions, fee_payer, blockhash_resp.value.blockhash
        )
        tx = Transaction.new_unsigned(message)
        sim = await self._rpc.simulate_transaction(tx, sig_verify=False)

        success = sim.value.err is None
        error_str = None
        if not success:
            logs_tail = "\n".join((sim.value.logs or [])[-4:])
            error_str = f"{sim.value.err}\n{logs_tail}"
            logger.warning("Simulation failed for %s: %s", instruction_name, error_str)

        return UnsignedTransaction(
            unsigned_tx_b64=base64.b64encode(bytes(tx)).decode(),
            fee_payer=str(fee_payer),
            instruction_name=instruction_name,
            program_id=str(self.program_id),
            simulation_success=success,
            simulation_error=error_str,
            units_consumed=sim.value.units_consumed,
        )
