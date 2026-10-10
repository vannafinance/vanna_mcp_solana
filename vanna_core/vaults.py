"""Basis and Perena vaults on the hosted fork: the app's `lib/solana/vault-catalogue.ts` (vanna-credit-backend main
`2462473`). Each vault mints a share token (six decimals) that the core program registers as an allowed collateral
token. This module is data only; pricing and actions build on it.
"""

from __future__ import annotations

from dataclasses import dataclass

from vanna_core.constants import PERENA_TRANCHE_STATE, PERENA_USDSTAR_VAULT, TSLAX_MINT, WSOL_MINT

BASIS_PROGRAM = "GtuQoYxzytGKtLqQGkYrVbsaPHTZN5jboP25DrSmKvGr"
PERENA_PROGRAM = "save8RQVPMWNTzU18t3GBvBkN9hT7jsGjiCQ28FpD9H"
SHARE_DECIMALS = 6


@dataclass(frozen=True)
class VaultInfo:
    id: str
    kind: str  # "basis" | "perena"
    name: str
    symbol: str
    asset: str  # the market it tracks (Basis) or USDC (Perena); every deposit is USDC
    mint: str  # the share token
    account: str  # the vault (Basis) or the Perena pool account holding its NAV
    asset_mint: str = ""
    lookup_table: str = ""
    price_at: int | None = None  # Perena: byte offset of the u64 NAV (6 decimals) and its i64 publish time


VAULTS: tuple[VaultInfo, ...] = (
    VaultInfo("basis-sol", "basis", "SOL Basis", "vbSOL", "SOL", "zMv5AVEURXUVBNmTqvV7ecgXun4uRAq8m3JM13iW71B",
              "BLeEcGjJnbGQyDBeTgckreKnKD87xFDDBFTfQYEyZ1j7", WSOL_MINT,
              "H3Ki7aLDD4Dt1gLkDbpFbgRcG8Pr3aDaRbRkxp83QztC"),
    VaultInfo("basis-eth", "basis", "ETH Basis", "vbETH", "ETH", "6Xin8G4Lq9RPxEDc1NnZ4JAkqwLstnosbnu3AeQavVuZ",
              "7PwzomohGgy1WQeDRaJQs31UQqhapJk67gCg4zoL7znv", "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
              "9ytyS5LxCUvG9AmiUpxywwfz12dzF9KsbMNhgRAaewmH"),
    VaultInfo("basis-btc", "basis", "BTC Basis", "vbBTC", "BTC", "7LgRZNY3XTLx95LL1YYz4q7cFLAwupwtEjYepBPvSJ1R",
              "6qFcw8qCq2oxh1iqpYAKLw3Z7tUMj6APP4tNuHyosveZ", "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
              "5kt2jtrgzVV5uQ6orvypUu9CmbNzdYLdxUovQGbuVkuT"),
    VaultInfo("basis-tsla", "basis", "TSLA Basis", "vbTSLA", "TSLA", "AzYyMurfA6r5hhBT2R294Zx6pMdeZfCYJ48AkcEnc43T",
              "J23xTNSTgiWdVMgcDbQgJMRc5MWhQ4Q2TrcnbDwG8ejw", TSLAX_MINT,
              "3ChTRD92PqR54CgkYvwwEKFBV5rP9bCbCojCUmJgVddA"),
    VaultInfo("basis-meta", "basis", "META Basis", "vbMETA", "META", "3EfoqbFDf7FSgtifJsyFiG1QFFStf72tm2n8iqyBh5pa",
              "3P5p9a4pr6NqRtZpbpPMhC2Kgg5AFjCHishuxP6FW1pJ", "Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu",
              "52iq7JhMeUmEUWehUeKPXyK4nPgYRx98QpwVDBQBkvF3"),
    VaultInfo("perena-usdstar", "perena", "USD*", "USD*", "USDC", "star9agSpjiFe3M49B3RniVU4CMBBEK3Qnaqn3RGiFM",
              PERENA_USDSTAR_VAULT, price_at=1584),
    VaultInfo("perena-junior", "perena", "USD* Junior", "USD* Junior", "USDC", "3AJYCjrhKitmBd8tRUurPA5dGHCAQh6A8L9DkuSC37Rm",
              PERENA_TRANCHE_STATE, price_at=256),
)

_BY_MINT = {v.mint: v for v in VAULTS}


def vault_by_mint(mint: str) -> VaultInfo | None:
    return _BY_MINT.get(mint)
