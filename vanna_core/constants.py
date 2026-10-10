"""Values more than one module needs, defined once: Solana's own limits, the program's sentinels and the well-known
mainnet addresses (mints and third-party programs). Plain values with no imports, so any module can use them without
an import cycle; each module wraps them in a `Pubkey` where it needs one.

Fork-only or deployment-specific addresses (the vault catalogue, lookup tables, program ids) are not here: they live
with their data (`vaults.py`) or in settings (`config.py`).
"""

# ── Solana limits ────────────────────────────────────────────────────────────────────────────────────
MAX_TX_BYTES = 1232  # one serialized transaction (the packet limit)
MAX_TABLE_ADDRESSES = 256  # addresses one lookup table can hold
EXEC_COMPUTE_UNITS = 1_400_000  # the compute budget asked for a margin_execute (Kamino/Jupiter ~1M, Phoenix ~600k)

# ── vanna_lending / vanna_oracle sentinels ───────────────────────────────────────────────────────────
EMPTY_ASSET_INDEX = 65_535  # an unused collateral/debt asset-index slot (the app's EMPTY_INDEX)
SCOPE_CHAIN_UNUSED = 0xFFFF  # an unused Scope price-chain entry

# ── mints ────────────────────────────────────────────────────────────────────────────────────────────
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
JUPSOL_MINT = "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v"
JUPUSD_MINT = "JuprjznTrTSp2UFa3ZBUFgwdAmtZCq4MQCwysN55USD"
TSLAX_MINT = "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB"
PHUSD_MINT = "PhUsd11YkbjSaWjFncfAAmatntsjx3MgDR9B6g1ks3A"  # Phoenix's canonical USDC (Ember-wrapped 1:1)

# ── third-party programs and accounts ────────────────────────────────────────────────────────────────
KLEND_PROGRAM = "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"
JUPITER_V6_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
PHOENIX_EXCHANGE = "2zskx2iyCvb6Stg7RBZkt1f6MrF4dpYtMG3yMvKwqtUZ"  # Phoenix's global config: the perps venue key
PERENA_USDSTAR_VAULT = "ECJGrTZ6QYMEwiEAnL4oReWF126uc22e9Lojy9qyCjHT"  # Perena's USD* vault (its NAV account)
PERENA_TRANCHE_STATE = "FbsiAonbNrC3EKWMrZxx9iSc5Q7e7zSAkro4tD8dQQJS"  # USD* Junior's tranche state (its NAV account)
