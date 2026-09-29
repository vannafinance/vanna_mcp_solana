"""Kamino Lend reserve reads — cToken exchange rate for valuing receipt
collateral. Byte offsets and formula are a direct Python port of
`adapters/kamino.rs::read_reserve_rate`, verified against the source and
cross-checked against live reserve accounts on the fork (both known
collateral mints decoded correctly, 2026-09-28).

This only reads Kamino's `Reserve` account — it never calls into the Kamino
program. `vanna_farm_kamino`'s writes go through `margin_execute`
(`vanna_core/contracts/margin_trade.py`'s sibling for the farm adapter, not
this module).
"""

from __future__ import annotations

from solders.pubkey import Pubkey

from vanna_core.client import SolanaClient
from vanna_core.types import ContractCallError

# klend `Reserve` account size and layout version these offsets are verified
# against — an unexpected layout fails closed rather than risk misreading
# prices, exactly like the on-chain adapter.
_RESERVE_LEN = 8624
_RESERVE_VERSION = 1
_RESERVE_DISCRIMINATOR = bytes([43, 242, 204, 202, 26, 247, 59, 127])


class ReserveRate:
    __slots__ = ("liquidity_mint", "liquidity_decimals", "total_liquidity", "collateral_supply")

    def __init__(self, liquidity_mint: str, liquidity_decimals: int, total_liquidity: int, collateral_supply: int) -> None:
        self.liquidity_mint = liquidity_mint
        self.liquidity_decimals = liquidity_decimals
        self.total_liquidity = total_liquidity
        self.collateral_supply = collateral_supply

    def underlying_for_receipts(self, receipts: int) -> int:
        """underlying = floor(receipts * total_liquidity / collateral_supply)."""
        if receipts == 0:
            return 0
        if self.collateral_supply <= 0:
            raise ContractCallError("Kamino reserve has zero collateral_supply — invalid_kamino_accounts")
        return (receipts * self.total_liquidity) // self.collateral_supply

    def receipts_for_underlying(self, underlying: int) -> int:
        """receipts = floor(underlying * collateral_supply / total_liquidity)."""
        if underlying == 0:
            return 0
        if self.total_liquidity <= 0:
            raise ContractCallError("Kamino reserve has zero total_liquidity — invalid_kamino_accounts")
        return (underlying * self.collateral_supply) // self.total_liquidity


def parse_reserve_rate(data: bytes, collateral_mint: Pubkey) -> ReserveRate:
    """Pure decode of a klend `Reserve` account's exchange rate (no I/O). Owner
    checking is the caller's job; everything else fails closed like the
    on-chain adapter."""
    if len(data) != _RESERVE_LEN:
        raise ContractCallError(f"Unexpected Kamino reserve length: {len(data)} != {_RESERVE_LEN}")
    if data[:8] != _RESERVE_DISCRIMINATOR:
        raise ContractCallError("Unexpected Kamino reserve discriminator")
    if bytes(data[2560:2592]) != bytes(collateral_mint):
        raise ContractCallError("Kamino reserve's collateral mint doesn't match the expected receipt mint")

    def u64_at(i: int) -> int:
        return int.from_bytes(data[i : i + 8], "little")

    def u128_at(i: int) -> int:
        return int.from_bytes(data[i : i + 16], "little")

    if u64_at(8) != _RESERVE_VERSION:
        raise ContractCallError(f"Unexpected Kamino reserve version: {u64_at(8)}")

    liquidity_decimals = u64_at(272)
    if liquidity_decimals > 18:
        raise ContractCallError(f"Implausible Kamino liquidity_decimals: {liquidity_decimals}")

    total_sf = (u64_at(224) << 60) + u128_at(232) - u128_at(344) - u128_at(360) - u128_at(376)

    return ReserveRate(
        liquidity_mint=str(Pubkey(bytes(data[128:160]))),
        liquidity_decimals=liquidity_decimals,
        total_liquidity=total_sf >> 60,
        collateral_supply=u64_at(2592),
    )


class KaminoContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client

    async def read_reserve_rate(self, reserve_address: Pubkey, klend_program_id: Pubkey, collateral_mint: Pubkey) -> ReserveRate:
        data = await self._client.fetch_raw_account_with_owner(reserve_address, klend_program_id)
        return parse_reserve_rate(data, collateral_mint)
