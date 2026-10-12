# Vanna Solana MCP — Full Tools Catalogue (v1, frozen for implementation)

> **Status (2026-09-29).** The team's decisions of 2026-09-29 are applied below and recorded, with
> their code consequences. In short: the target is the hosted
> Surfpool fork; the connected wallet signs (no Sign Service, `vanna_sign` is `tx_status` only);
> prices come from the app's keeper (no Hermes/`pyth-pull` this release); only SOL/USDC/USDT are
> lent/borrowed; liquidation stays read-only. This catalogue's on-chain detail (§1–§2) describes the
> program version in `vanna-credit-backend/idl` (`PriceSource`/`PriceUpdateV2` oracle model), which
> was the deployed program when this catalogue was frozen. The owner has since confirmed the fork runs
> layer HEAD `ecdb9b6`, and the MCP code is ported to it (`OracleConfig`,
> Scope-first pricing, no price accounts on borrow/withdraw/`margin_execute`, whole-account
> liquidation). Where §1–§2 describe `PriceSource`/`PriceUpdateV2` mechanics, the code and
> the ported code win. The tool surface (§3) is unchanged.

This is the Solana counterpart of the Stellar/Soroban `vanna-mcp` tools reference
(`Vanna_MCP/vanna-mcp/README.md` → "Tools reference"). The technique is the same:

- Tools are grouped by user-facing feature.
- Read tools return live state.
- Write tools return **unsigned transactions and a simulation result**, never a signature.

The on-chain mechanics are Solana-native:

- One Anchor program (`vanna_lending`), with PDAs instead of per-user contracts.
- SPL Token instead of SEP-41.
- Pyth `PriceUpdateV2` accounts instead of Reflector.
- Kamino Lend and Jupiter v6 (called through `margin_execute`) in place of Stellar's Blend, Aquarius and Soroswap.

**Sources of truth, in order of authority**

| Source | What it defines |
|---|---|
| `vanna-credit-layer/programs/vanna_lending/src/**` (working tree on `aae7659`) | **The authority** for instruction accounts, arguments, math and on-chain rules (`constants.rs`, `math/*.rs`, `risk_engine.rs`, `validation/accounts.rs`, `oracle/*.rs`, `adapters/*.rs`) |
| `vanna-credit-layer/target/idl/vanna_lending.json` (byte-identical to `vanna-credit-backend/idl/vanna_lending.json`, SHA-256 `b438bfdd66281209440c93cfe0f2f1bbd7bdc65cfa2fccd7b652215851cd32d4`) | The generated contract interface: 27 instructions, 6 accounts, 26 events, 56 errors |
| `Vanna-Solana-Copilot-Answers.pdf` (28 Sep 2026, in `vanna-credit-backend/docs/copilot/`) | The team's answers to the Part A build questions and Part B wrapper requirements. It uses the labels **Verified**, **Proposed** and **Pending**, and this catalogue keeps them. |
| `vanna-credit-backend` @ `27e6129` (`lib/solana/*`, `app/api/*`) | **An evolving implementation reference**: how the app assembles transactions, the fork price keeper, history. It is not a finished API contract. Its helpers sign and broadcast, so the MCP reuses its *instruction construction* only (§2.2). |

`Backend-Solana` / `Backend-Solana-v2` are not references; they belong to the old stack.

---

## 0. What changed

### 0.1 Since the credit-layer-scripts version

| Area | Now | Impact on the MCP |
|---|---|---|
| Network | **Surfpool mainnet fork** (real mainnet mints, Kamino and Jupiter accounts). The backend defaults to the hosted fork `https://rpc-devnet.solana.vanna.finance`; the layer scripts default to a local Surfpool `http://127.0.0.1:8899` / `ws://127.0.0.1:8900`. The target cluster is the hosted fork. | RPC/WS and cluster label are config, never constants. Blockhashes, reads and broadcasts use the same configured RPC. |
| Program ID | **`H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC`** in `declare_id!`, `Anchor.toml`, the script PDA helpers and the backend default (**Verified**). The layer README's `BZ812…` is stale. | Config value, default `H37YVBG3…`. Before writes are enabled, verify the deployed executable matches the pinned IDL (§7). |
| Assets | **Product = the app's token registry: SOL (wSOL), USDC, USDT, JitoSOL, JupSOL, JupUSD and the Token-2022 xStocks NVDAx, GOOGLx, AAPLx, TSLAx, + the 5 Kamino receipts.** A registered asset with no name (and the GMTrade venue) is hidden and refused (`asset_not_offered`) while any position in it still counts. Lend/borrow: SOL, USDC, USDT only; the rest are margin collateral and swap only. USDT is not a collateral deposit; the JitoSOL/JupSOL Kamino farms are closed to new supply (redeem allowed) | §1 table is a display catalogue; authoritative parameters come from on-chain `AssetConfig` (§1.3). The MCP refuses supply/borrow of JitoSOL/JupSOL with `asset_not_offered` |
| Token programs | Classic SPL for all ten. The program still supports Token-2022 (per-asset `token_program`). | Always pass the token program explicitly; derive outcomes from measured received amounts |
| Prices | The fork has **no live Pyth pusher**; the app's price keeper rewrites the oracle accounts with Jupiter USD prices via cheatcodes. That is the mechanism for this release, not production oracle publication. | The MCP calls the keeper before every health-checked build (§2.3). No Hermes/`pyth-pull` this release |
| Transaction format | **Always v0.** 600k CU for batches, 1.4M for `margin_execute`. Over 1232 bytes, lookup-table **setup transactions** are required first. | Envelope schema v1 with explicit dependencies (§2.1) |
| Farm | Kamino single-asset supply/withdraw from the **margin balance** via `margin_execute`; the Farm UI has no wallet funding or leverage. Kamino Liquidity impossible (`CPINotAllowed`). | `vanna_farm_kamino.deposit` / `redeem` |
| Swaps | Margin swaps (`margin_execute` + Jupiter) and wallet swaps | `vanna_swap.margin_swap`, `vanna_swap.wallet_swap` |
| History | Decoded from Anchor events (no indexer) | `vanna_history` |

### 0.2 From the answers PDF (corrections applied in this version)

| Topic | Answer | Status | Where applied |
|---|---|---|---|
| Builders vs execution | The backend's `sendInstructions` compiles, simulates, **asks for signatures, broadcasts and confirms**; it returns a signature. It cannot be an unsigned-only builder. The MCP must never call `sendInstructions`, `.rpc()`, `signTransaction` or `sendRawTransaction`. | Verified | §2.2, §5 |
| Envelope | Schema v1: `schemaVersion`, `cluster`, `programId`, `feePayer`, `transactions[{id, dependsOn, format, encoding, unsignedTransaction, blockhash, lastValidBlockHeight, requiredSigners, simulation}]`, `summary`. All integers as decimal strings. Simulation status `success` / `failed` / `not_run_dependency`. | Proposed (adopted) | §2.1 |
| Dependencies | A dependent action is never marked successful because its setup was simulated. When setup must be confirmed first, return the dependency plan and **rebuild and re-simulate** the action afterwards. | Proposed (adopted) | §2.1 |
| Asset discovery | Static catalogue only for symbols/icons. Authoritative assets and parameters from validated `AssetConfig` accounts (owner, discriminator, PDA). An unknown active index **stops the action** with an unsupported-asset error. | Proposed (adopted) | §1.3, §2.5 |
| Risk fields | `ltv_bps` and `liquidation_threshold_bps` are stored/validated but **do not weight** the health factor. The liquidation bonus is active. | Verified | §1.2, §2.4 |
| HF threshold | `BALANCE_TO_BORROW_THRESHOLD_WAD = 1.1e18`, global. Equality is liquidatable. Zero debt = u128-max sentinel → report "no debt". The backend's 1.15 is **UI policy**. The constant can change in a future program version. | Verified | §2.4 |
| Margin arrays | `[u16; 24]` each, sentinel 65535, **not densely packed**; scan all slots | Verified | §2.5 |
| `user_deposit_and_borrow` | Absent from source and IDL; compose instead. Not atomic if setup splits it across transactions. | Verified | §3.5 (`vanna_margin_trade.deposit_and_borrow`) |
| Interest | Polynomial `RateCurve` (`linear_coeff_wad`, `jump_coeff_wad`, `rate_multiplier_wad`), not base/slope/kink | Verified | §2.8 |
| Margin vaults | ATAs (`allowOwnerOffCurve = true`) with the asset's token program; not Vanna PDAs | Verified | §1.1 |
| Oracle | Builders prepare fresh oracle inputs for **every relevant position** in borrow, withdraw, composed deposit/borrow, liquidation and `margin_execute`. Reuse fresh push accounts. Never broadcast oracle updates from the MCP. | Proposed (adopted) | §2.3 |
| Liquidation | `public_liquidate` is permissionless on-chain. **v1: user-Copilot liquidation execution disabled**; eligibility reads only; an optional keeper builder is isolated and off by default. | Proposed (adopted) | §3.4 (`vanna_margin_status.liquidation_snapshot`), keeper builder noted after §3.15 |
| Admin | **No admin wrappers in v1.** Read-only protocol/asset/reserve/integration config only. | Proposed (adopted) | §4 |
| Faucet | Fork-only dev feature (`POST /api/faucet`, cheatcodes, not a transaction), disabled in production | Proposed (adopted) | §3.15 |
| Swap / farm | Ship core lending/margin first; swap/farm after the backend workflows stabilise. **Receipt-aware health reads from day one.** No arbitrary program invocation. | Proposed (adopted) | §7 phases |
| Protocol modes | Follow each instruction's real checks (`validation/accounts.rs`); `public_liquidate` has **no** `ProtocolConfig` account, so no mode gate applies to it | Verified | §2.6 |

---

## 1. Deployment, programs and accounts

### 1.1 Deployment and PDAs

| Item | Value |
|---|---|
| Cluster | Surfpool mainnet fork. Config: `SOLANA_RPC_URL`, `SOLANA_WS_URL`, `SOLANA_CLUSTER_LABEL` (default `localnet-fork`). Launch cluster **Pending**. |
| Program | `vanna_lending` `H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC` (env `VANNA_PROGRAM_ID`): 27 instructions (12 admin, 3 lending pool, 12 account manager) |
| IDL pin | SHA-256 `b438bfdd…32d4`, computed over the file with **LF line endings** (a Windows CRLF checkout of the same file hashes to `2125e784…`). The server normalises line endings, hashes, and refuses to start on a mismatch. Checked 28 Sep 2026: the backend copy matches; the layer's `target/idl` isn't built locally; **this repo's current `idl/vanna_lending.json` (`e602f07a…`) is the old Backend-Solana IDL and must be replaced**. |
| Protocol setup | `vanna-credit-layer/scripts/bootstrap-fork.sh` |
| Explorer | `explorer.solana.com/{tx|address}/…?cluster=custom&customUrl=<public RPC>` |

| Account | Seeds / derivation | Role |
|---|---|---|
| `ProtocolConfig` | `["protocol"]` | `admin`, `pending_admin`, `treasury`, `operating_mode` (Normal 0, BorrowPaused 1, WithdrawOnly 2, Halted 3), `max_assets_per_margin`, `next_asset_index` |
| `AssetConfig` | `["asset", mint]` | Mint, `token_program`, reserve, `price_feed_id`, `price_source` (`Pyth` / `KaminoReceipt`), `price_source_account`, `price_source_program`, `max_collateral_per_margin`, `max_price_age_secs`, `max_confidence_bps`, `ltv_bps`*, `liquidation_threshold_bps`*, `liquidation_bonus_bps`, `asset_index`, decimals, `collateral_enabled`, `borrow_enabled` (*stored, not used in HF) |
| `Reserve` | `["reserve", mint]` | `asset_config`, liquidity vault, share mint, caps, `accounted_liquidity_assets`, `total_borrow_assets/shares`, `accrued_protocol_fees`, `last_update_timestamp`, `rate_curve {linear_coeff_wad, jump_coeff_wad, rate_multiplier_wad}`, `reserve_factor_bps`, `status` (Active 0, SupplyOnly 1, RepayOnly 2, Frozen 3) |
| share mint | `["share_mint", mint]` | Classic SPL; its supply is the share total for exchange rates |
| `MarginAccount` | `["margin", authority]` | One per wallet: `authority`, `status`, `collateral_count`, `debt_count`, `event_sequence`, `collateral_asset_indexes[24]`, `debt_asset_indexes[24]` (empty = 65535, sparse) |
| `DebtPosition` | `["debt", margin, reserve]` | `margin_account`, `reserve`, `borrow_shares` |
| `Integration` | `["integration", external_program_id]` | `program_id`, `adapter` (Kamino / Jupiter), `enabled` |
| reserve vault | ATA(mint, owner = reserve PDA, token program) | Pool liquidity |
| margin vault | `getAssociatedTokenAddressSync(mint, marginPda, true, tokenProgram)` | **Its live balance is the collateral**; borrowed funds land here. No separate collateral-position account exists. |
| lender share account | ATA(share mint, lender, SPL) | Earn position |
| price account | PDA(`[0u8, 0u8] ‖ feed_id`, Pyth push oracle program) | The `PriceUpdateV2` the program reads. Don't confuse feed ID (hex), price account (address) and receiver owner (program). |

### 1.2 Display catalogue (registration defaults; live values come from `AssetConfig`)

| Key | Mint | Dec. | Program | Price source | LTV* / LT* / bonus (bps) |
|---|---|---|---|---|---|
| `SOL` | `So11111111111111111111111111111111111111112` | 9 | SPL | Pyth `ef0d8b6f…b56d` | 7000 / 8000 / 500 |
| `USDC` | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | 6 | SPL | Pyth `eaa020c6…c94a` | 8000 / 8500 / 500 |
| `USDT` | `Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB` | 6 | SPL | Pyth `2b89b9dc…e53b` | 8000 / 8500 / 500 |
| `JitoSOL` | `J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn` | 9 | SPL | Pyth `67be9f51…9ffb` | 6500 / 7500 / 500 |
| `JupSOL` | `jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v` | 9 | SPL | **Synthetic sentinel** `6cf9d010…c0c1` (fork-only) | 6500 / 7500 / 500 |
| `kSOL` | `2UywZrUdyqs5vDchy7fKQJKau2RVyuzBev2XKGPDSiX1` | 6 | SPL | KaminoReceipt (SOL feed), reserve `d4A2prbA2whesmvHaL88BH6Ewn5N4bTSU2Ze8P6Bc4Q` | 5000 / 6000 / 500 |
| `kUSDC` | `B8V6WVjPxW1UGwVDfxH2d2r8SyT4cqn7dQRK6XneVa7D` | 6 | SPL | KaminoReceipt (USDC), `D6q6wuQSrifJKZYpR1M8R4YawnLDtDsMmWM1NbBmgJ59` | 5000 / 6000 / 500 |
| `kUSDT` | `B8zf4kojJbwgCRKA7rLaLhRCZBGhgAJp8wPBVZZHMhSv` | 6 | SPL | KaminoReceipt (USDT), `H3t6qZ1JkguCNTi9uzVKqQ7dvt2cum4XiXWom6Gn5e5S` | 5000 / 6000 / 500 |
| `kJitoSOL` | `9ucQp7thL38MDDTSER5ou24QnVSTZFLevDsZC1cAFkKy` | 6 | SPL | KaminoReceipt (JitoSOL), `EVbyPKrHG6WBfm4dLxLMJpUDY43cCAcHSpV3KYjKsktW` | 5000 / 6000 / 500 |
| `kJupSOL` | `8euYpQt56z9hA2ZcFCQuWK6gAptrLcJuMZ5PtMpUwbuY` | 6 | SPL | KaminoReceipt (JupSOL), `DGQZWCY17gGtBUgdaFs1VreJWsodkjFxndPsskwFKGpp` | 5000 / 6000 / 500 |

\* LTV and liquidation-threshold fields are compatibility fields; they don't change the health formula (§2.4). Defaults for all assets: `max_price_age_secs` 3600, `max_confidence_bps` 1000. Receipts are collateral-only (`borrow_enabled = false`); there is no Vanna borrowing pool per receipt mint. All receipts sit in the Kamino main market `7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF`.

**Script-supported, not launch assets** (Appendix A of the answers): TSLAx, GOOGLx, AAPLx (xStocks, Token-2022, Pyth) and ANTHROPIC, OPENAI (PreStocks, Token-2022, synthetic fork feeds), plus `ktslax` / `kgooglx` receipts. They are **not** in the display catalogue. If any appears on chain, discovery (§1.3) still values it correctly or stops with an unsupported-asset error; it is never silently skipped.

### 1.3 Asset discovery (runtime)

1. Keep the static table above only for symbol, icon and display decimals.
2. Build `asset_index → AssetConfig → mint` from chain state: `AssetConfig` PDAs for the catalogue mints via `getMultipleAccounts` (never `getProgramAccounts` on the fork, which is rate-limited upstream).
3. Validate each account: owner = program, Anchor discriminator, PDA re-derivation from the mint.
4. When a margin account holds an `asset_index` not in the map, resolve it (bounded lookup by index up to `next_asset_index`); if it still can't be validated, the tool fails with `code: "unsupported_asset"`. **A static catalogue must never cause an active position to be omitted from a health check.**
5. Symbol lookup is case-insensitive; `wSOL` is accepted as `SOL`.

### 1.4 External programs

| Program | Role |
|---|---|
| Pyth Receiver `rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ` | Owns every `PriceUpdateV2` |
| Pyth push oracle `pythWSnswVUd12oZpeFP8e9CVaEqJg25g1Vtc2biRsT` | Derives price account addresses (shard 0) |
| Kamino Lend `KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD` | Farm (`deposit_reserve_liquidity`, `redeem_reserve_collateral`); its `Reserve` prices cTokens |
| Jupiter v6 `JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4` | Swaps; `margin_execute` allows only `route` and `shared_accounts_route` |
| SPL Token, Token-2022, Associated Token, System, Compute Budget, Address Lookup Table, Instructions sysvar | Standard |

---

## 2. Shared mechanics every tool builds on

### 2.1 Write-tool envelope (schema v1)

Write tools take amounts as decimal strings in UI units (`"1.5"`) or `"all"`. They return integers as **decimal strings, never floats** (u64 and u128 included).

```json
{
  "schemaVersion": "1",
  "cluster": "localnet-fork",
  "programId": "H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC",
  "feePayer": "<wallet>",
  "transactions": [{
    "id": "action-1",
    "purpose": "deposit_and_borrow",
    "dependsOn": [],
    "format": "solana-v0",
    "encoding": "base64",
    "unsignedTransaction": "<serialized v0 message with empty signatures>",
    "blockhash": "<recent blockhash>",
    "lastValidBlockHeight": "<integer>",
    "requiredSigners": ["<wallet>"],
    "addressLookupTables": ["<address>"],
    "simulation": { "status": "success", "err": null, "logs": [], "unitsConsumed": "<integer>", "contextSlot": "<integer>" }
  }],
  "summary": { "action": "borrow", "mint": "<mint>", "amountRaw": "1000000" },
  "preview": { "healthFactorBefore": "1.84", "healthFactorAfter": "1.32", "noDebt": false },
  "signing_status": "needs_wallet_sign",
  "error": null, "code": null, "reason": null, "simulation_success": true
}
```

Rules:

- **`simulation.status`** is `success`, `failed` or `not_run_dependency`. The top-level `simulation_success` is true only if **every** transaction has `success`.
- **Dependencies.** When the action needs setup that must be **confirmed** before the action can be simulated (a temporary lookup table; a production oracle post that can't fit), the tool returns the setup transactions (`dependsOn: []`, simulated) and the action with `status: "not_run_dependency"` and **no `unsignedTransaction`**. After the caller confirms the setup, it calls the same tool again with `prepared: { addressLookupTables: [...] }`; the tool rebuilds and re-simulates the action. An expired blockhash also means rebuild and re-simulate.
- **Lookup-table setup** (over 1232 bytes): legacy create + extend transactions (20 addresses per extend), wallet as authority and payer. The table is usable from the slot after its last extension.
- **`requiredSigners`** lists public keys only. **The envelope never contains private keys.** If a production oracle post needs ephemeral signers, those are created and held by the execution layer, not the MCP (§2.3).
- **`signing_status`, `code`, `reason`, `simulation_success`** keep the Stellar meanings the Copilot executor branches on (`code` / `reason` / `simulation_success = false` = rejected before broadcast). `signing_status` is `needs_wallet_sign` (every built action — the connected wallet signs) or `not_signable` (setup dependency outstanding). Stellar's `auto_signed` value was removed from the Solana schema (`vanna_core/envelope.py`): there is no auto-sign.
- **Execution adapter response** (the app, not the MCP): `{ requestId, status: "awaiting_approval" | "submitted" | "confirmed" | "failed", signatures, error }`.

### 2.2 Transaction assembly (instruction construction only)

The backend's service functions mix instruction construction with signing and sending (`sendInstructions`, `TxSigner`). The MCP ports **only the construction**: the same instructions, accounts, order and bounds. It then compiles an unsigned v0 message and simulates. It never signs, sends or confirms.

1. **Compute budget.** `SetComputeUnitLimit(600_000)` for batched Vanna instructions; `1_400_000` for any `margin_execute`; none for single simple instructions.
2. **Native SOL on the wallet side.**
   - *Paying in* (deposit, supply, wallet repay): idempotent wSOL ATA, `SystemProgram.transfer`, `SyncNative`; close the ATA at the end if this transaction created it.
   - *Receiving* (withdraw, redeem): a temporary seeded token account (`createAccountWithSeed` + `InitializeAccount3`, owned and funded by the wallet, so no extra signer) closed in the same transaction.
3. **Destination ATAs.** `createAssociatedTokenAccountIdempotent` with the asset's token program.
4. **Wallet "max".** Keeps 0.05 SOL back for fees and rent.
5. **Simulation.** Against the same blockhash, `sigVerify: false`, `replaceRecentBlockhash: false`. On a fork, a failed simulation may trigger the stale-DEX heal (§2.7) and one retry. Errors are decoded from the IDL (56 program errors) plus the Jupiter/stale-pool classifiers, and summarised in one line.
6. **Token-2022 readiness.** Never assume received = sent; the preview uses the post-transfer-fee amount where a mint has a fee extension. Repay-all grosses up for transfer fees.

### 2.3 Oracle preparation

Builders for `user_borrow`, `user_withdraw_collateral`, composed deposit + borrow, `public_liquidate` and `margin_execute` prepare fresh prices for **every price the instruction reads** (the named assets and every position in the §2.5 scan), not only the requested asset. Price freshness is judged against each asset's live `max_price_age_secs` on the **chain clock** (the Clock sysvar), not wall time.

| Adapter | When | Behaviour | Status |
|---|---|---|---|
| `dev-fork`, `PRICE_REFRESH_SOURCE=keeper` | Default; this release | Calls the app's price keeper (`POST {PRICE_KEEPER_URL}/api/prices/refresh`: Jupiter USD price → `surfnet_setAccount`, stamped with the fork clock, with a previous/reference-price fallback; also heals cloned Kamino reserves). At most once per 60 s per process, concurrent callers share one call — the app's own `ensureFreshPrices` rule. No price instructions in the transaction. A failed refresh is logged and the build continues, so a still-stale price fails closed in simulation (`stale_price`). Development-only mechanism; JupSOL is priced from Jupiter here. | Built (`oracle_refresh.py`) |
| `dev-fork`, `PRICE_REFRESH_SOURCE=cheatcode` | Only for a program version the keeper does not cover | The MCP's own `surfnet_setAccount` writer: fabricates the Pyth accounts each asset pins and copies/restamps Kamino Scope from mainnet. Never run alongside the keeper — both write the same accounts. | Built, off by default |
| `pyth-pull` | Real cluster with external keepers | No refresh at all. A Hermes-backed production adapter is **not built and not planned for this release**; public-mainnet oracle requirements are separate. | Not in scope |

**Keeper supplement (live findings, 2026-09-29/30).** After the keeper call the MCP adds two narrow cheatcode
writes, each guarded so it never overlaps the keeper or fakes freshness:

- **Pyth-only feeds the keeper does not write** (JupUSD today, which has no Scope chain): refreshed from Jupiter,
  never an account the keeper reported writing.
- **Scope entries stale only because of fork clock drift.** The keeper copies Kamino's Scope account from mainnet
  and restamps its own assets' entries; others (the xStocks) keep mainnet timestamps, and the fork's clock has been
  seen ~15 min ahead of real time, so current prices read as stale. An entry is restamped only if it is stale by
  the fork clock **and** within its max age by real time; a feed that really stopped updating stays stale and the
  program fails closed.

Validation on every read (both modes): owner = receiver, discriminator/layout, verification level, feed ID = `AssetConfig.price_feed_id`, price > 0, age ≤ `max_price_age_secs`, confidence ≤ `max_confidence_bps`. Use the receiver SDK's decoder; the fork helper's fixed offsets aren't a general decoder. Receipts additionally need their validated Kamino source reserve.

### 2.4 Health model (authority: `constants.rs`, `math/health.rs`, `risk_engine.rs`)

**Valuation**

- `collateral` = Σ over active collateral slots of `vault balance × price`, **rounded down**. Borrowed funds sitting in margin vaults count.
- A Kamino cToken is first converted to underlying units at the reserve exchange rate (`adapters/kamino.rs::read_reserve_rate`), then priced with the underlying's feed. Never 1:1.
- `debt` = Σ over debt positions of `ceil(borrow_shares × accrued total_borrow_assets / total_borrow_shares)` × price, **rounded up**, with reserve interest projected to the chain Clock time first (§2.8). Other reserves are accrued in memory.

**The rule**

- `HF = collateral / debt`. Threshold `BALANCE_TO_BORROW_THRESHOLD_WAD = 1_100_000_000_000_000_000` (WAD = 10¹⁸), one global constant.
- Healthy: `debt = 0` **or** `HF > 1.10`. Liquidatable: `debt > 0` **and** `HF ≤ 1.10`; **equality is liquidatable**.
- Zero debt is the on-chain u128-max sentinel. Tools report `noDebt: true` and `healthFactor: null`, never a floating-point infinity.
- `ltv_bps` and `liquidation_threshold_bps` do not change the formula.
- The constant is fixed in this program version but not promised forever; the MCP reads it from its pinned program version, never from a frontend constant.

**Policy floor (not a protocol rule)**

- The 1.15 floor is the app's UI policy (`MIN_HEALTH_FACTOR`). Tools report values at both 1.15 (policy) and 1.10 (protocol, `risk.LIQUIDATION_HF`, derived from `BALANCE_TO_BORROW_THRESHOLD_WAD`).
- max borrow `B ≤ (C − h·D)/(h − 1)` (or `C/h − D` if the borrowed asset isn't collateral-enabled); max withdraw `W ≤ C − h·D`; max leverage on fresh equity `h/(h − 1)`.

**Failure behaviour.** Fail closed on missing accounts, unsupported price source, stale price, confidence breach, decode or RPC error. An approximate UI summary is never used as a borrow-authorisation engine; simulation is the final check.

### 2.5 Health-check `remaining_accounts` (authority: `risk_engine.rs::scan_and_validate_positions`)

> **Since the 2026-10-09 program upgrade** (implemented in `vanna_core/remaining_accounts.py` and
> `margin_state.py`): the collateral groups are one `[asset_config, margin_vault]` per token on the protocol's
> `ProtocolConfig.allowed_tokens` list (in that order, zero balances included), then one `[venue asset_config, margin]`
> per venue the margin holds, then the debt groups; the oracle segment prices only tokens held (and the
> instruction's own). `margin_execute` lost `new_assets` and gained `event_authority` + `program`. The text below is the
> earlier per-margin-slot layout, kept for history.

Walk **all 24 slots** of each array in stored order, skipping 65535 (the arrays are sparse):

- first, per active collateral: `[asset_config, margin_vault, price_update]`, plus `[price_source_account]` for non-Pyth (receipt) collateral;
- then, per active debt: `[asset_config, reserve, debt_position, price_update]`.

Mirror each instruction's exclusions exactly (the instruction values these through named accounts):

| Instruction | Excluded from the scan |
|---|---|
| `user_borrow` | the borrowed asset's collateral **and** its debt |
| `user_withdraw_collateral` | the withdrawn asset's collateral |
| `margin_execute` | the spent **and** the received collateral |
| `public_liquidate` | the named debt position and seized collateral (per `liquidate.rs`) |

Rules: a mint that is both collateral and debt appears in **both** groups. Never deduplicate, sort by symbol, skip unknown indexes (→ `unsupported_asset`, unlike the script helper) or append extra accounts. The risk engine rejects missing, substituted, reordered or extra accounts.

**Batches.** `MarginIndexModel` replays each earlier instruction's effect on a copy of the arrays so later instructions see the post-instruction state: `add_active_*` fills the first empty slot; `remove_active_*` empties in place; a borrow activates the debt, and the collateral if the vault was empty and the asset is collateral-enabled; `margin_execute` removes a drained spent vault and adds a funded received vault.

### 2.6 Pre-flight gates and slippage bounds

Gates are read from chain state and applied per instruction, as `validation/accounts.rs` and each instruction's own checks define them. A mode name is not assumed to block every action.

| Gate | Source |
|---|---|
| `ProtocolConfig.operating_mode` | Per-instruction checks. `public_liquidate` takes no `ProtocolConfig`, so no mode gate applies to it. |
| `Reserve.status` | Per-action gates for supply, redeem, borrow, repay |
| `collateral_enabled` / `borrow_enabled` | Deposit, the received leg of `margin_execute`, open debt, borrow |
| Caps | `supply_cap`, `borrow_cap`, `max_collateral_per_margin` (0 = uncapped) |
| Slot limit | `ProtocolConfig.max_assets_per_margin` and the 24-slot arrays |
| Wallet balance | Deposit, supply, wallet repay; SOL net of the 0.05 SOL reserve |
| Pool liquidity | Borrow ≤ projected `accounted_liquidity_assets`; redeem ≤ available lender cash (§2.8) |
| First supply | ≥ 1,000 raw shares |
| `margin_execute` | Spent asset must be active collateral; received asset collateral-enabled; integration enabled |
| Withdraw `min_health_factor_wad` | Optional caller floor; it cannot relax the protocol threshold |

| Bound | Value |
|---|---|
| `lender_supply` `min_shares_out` / `lender_redeem` `min_assets_out` | Expected × (1 − 0.5%) |
| `user_borrow` `max_debt_shares` | Expected shares + 0.5% + 1 |
| Kamino deposit/redeem `min_received` | Expected × (1 − 0.5%) |
| Jupiter `min_received` | The quote's `otherAmountThreshold` (default 100 bps slippage) |
| Wallet `repay_all` on SOL | Debt + 0.2% to cover accrual |
| `public_liquidate` `min_collateral_out` | Required; computed from the pre-check with the caller's slippage |

### 2.7 Fork-only operations

- **Stale DEX pools.** Cloned CLMM/DLMM/Whirlpool accounts drift. On a failed simulation, re-clone every such account the transaction touches (`surfnet_resetAccount`) and retry once. This changes chain state, not the transaction. Fork-only; disabled when `SOLANA_CLUSTER_LABEL` isn't a fork.
- **Jupiter routing.** Fork-safe DEX allowlist (`Raydium, Raydium CLMM, Raydium CP, Orca V2, Meteora DLMM, Whirlpool`, env `JUPITER_FORK_DEXES`); server-side `JUPITER_API_KEY`.
- **No indexer.** History = `getSignaturesForAddress(owner)` → one `getTransaction` per signature (Surfpool rejects batched JSON-RPC) → Anchor `EventParser`.

### 2.8 Reserve math (authority: `math/interest.rs`, `math/shares.rs`)

All in integer fixed point, mirroring the contract's rounding:

- Utilisation `u = total_borrow_assets / (accounted_liquidity_assets + total_borrow_assets)`; 0 for an empty pool.
- Annual borrow rate = `multiplier × [linear × (u + u³²) + jump × u⁶⁴]`, coefficients at WAD scale. Mirror `borrow_rate_per_second_wad` exactly.
- Year = 31,556,952 s. Interest over one interval = `ceil(debt × per_second_rate × elapsed / WAD)` (simple within the interval).
- Protocol fee = `floor(interest × reserve_factor_bps / 10_000)`.
- Lender assets = accounted liquidity + accrued borrow assets − accrued protocol fees (vault donations excluded).
- Redeem output = `floor(shares × lender_assets / share_supply)`; first supply is 1:1 with a 1,000-raw-share minimum.
- Available lender cash = `max(accounted_liquidity − accrued_protocol_fees, 0)`.
- Supply rate shown as **APR** = borrow APR × u × (1 − reserve factor), the backend's display convention. Never labelled APY unless a compounding assumption is stated.

---

## 3. Tool catalogue — 14 dispatcher tools (+1 dev-only)

**Shape decision (2026-09-28):** the MCP's exposed tool surface is **14 dispatcher
tools**, each taking an `action` string parameter and action-specific args —
not one flat tool per instruction/read. This mirrors how the Stellar MCP
actually runs in production (14 dispatchers) and matches the tool list in
`vanna-credit-backend/docs/copilot/COPILOT_ARCHITECTURE.md` §7, which was written against this exact action set. Every
action below is unchanged in mechanics from the flat-tool draft this replaces
— only the grouping and calling convention changed. **Mirrors** names the
`lib/solana` function whose *instruction construction* the action must match
(instruction order, accounts, bounds). **Phase** is the delivery phase from
§7.

### 3.1 `vanna_oracle` — read

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `price` | 1 | Validated on-chain `PriceUpdateV2` | USD, publish time, age on chain clock, `stale`, account address, `source: "pyth" \| "synthetic-dev"`. cTokens report underlying × rate. |
| `prices` | 1 | Same, batched | All discovered assets, one `getMultipleAccounts` |

The price refresh (§2.3) is internal — every health-checked write action
refreshes prices through the keeper first; there is no separate "refresh
price" action.

### 3.2 `vanna_protocol_info` — read

| Action | Phase | Purpose |
|---|---|---|
| `config` | 1 | Mode, `max_assets_per_margin`, `next_asset_index`, admin, pending admin, treasury |
| `assets` | 1 | Discovered assets (§1.3) with `AssetConfig` + `Reserve`: index, token program, price source, flags, caps, bonus, status |
| `integrations` | 1 | `Integration` PDAs: program, adapter, enabled |

### 3.3 `vanna_account` — read + write

| Action | Type | Phase | Mirrors | Purpose |
|---|---|---|---|---|
| `resolve` | read | 1 | `marginPda` | Whether the wallet has its one margin account, and its address |
| `get` | read | 1 | `fetchMarginAccount` | Status, counts, all active slots (sparse scan) mapped to assets, `event_sequence` |
| `health` | read | 1 | `fetchMarginSnapshot` | Per-position rows including receipts, totals, HF (or `noDebt`), `stale_prices`, max borrow/withdraw/leverage at 1.15 and 1.10 |
| `create` | write | 2 | `user_create_margin` | Returns the address if it already exists |
| `close` | write | 2 | `user_close_margin` | Only with an empty active-position registry |

### 3.4 `vanna_margin_status` — read

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `collateral` | 1 | Snapshot `collateral[]` | Vault balances, USD, active flag; cTokens at the Kamino rate |
| `debt` | 1 | Snapshot `debts[]` | Debt accrued to chain time, USD, borrow APR |
| `can_borrow` | 1 (pre-flight) | `maxBorrowUsd` + gates + liquidity | Fails closed |
| `can_withdraw` | 1 (pre-flight) | `maxWithdrawUsd` + gates | Fails closed |
| `liquidation_snapshot` | 1 | See old `vanna_check_liquidatable` | Complete validated snapshot: `debt > 0` and `HF ≤ 1.10`? Max repay = 50% of the selected debt position (1 raw unit minimum cap for dust); expected seize with the bonus, capped at the vault. Denies on any missing account, unsupported price source, stale price or read error. Eligibility read only — the Copilot never executes liquidation. |

### 3.5 `vanna_margin_trade` — write

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `deposit` | 2 | `depositCollateral` | One or more assets in one transaction; wraps SOL; no price check needed |
| `withdraw` | 2 | `withdrawCollateral` | Oracle prep (§2.3); scan excluding this asset; receipts pass `price_source_account`; SOL to a temporary receiver |
| `close_collateral` | 2 | `closeCollateralPosition` | Zero vault balance only |
| `open_debt` | — | — | Removed with the credit-layer split: `borrow` opens its own debt position |
| `borrow` | 2 | `borrow` | Oracle prep; opens debt before the first borrow; funds land in the margin vault and count as collateral |
| `close_debt` | 2 | `closeDebtPosition` | Zero shares only |
| `repay` | 2 | `user_repay_from_margin(max_assets, repay_all)` | Balance-limited; no price scan. After it lands, **re-read** remaining debt; `repay_all` doesn't guarantee closure. Close is a separate action (only when the preview shows zero shares). |
| `repay_from_wallet` | 2 | `public_repay_from_wallet` | Payer may repay any margin account; wraps SOL (+0.2% for `all`) |
| `deposit_and_borrow` | 2 | optional `user_create_margin` → N × deposit → per asset open debt + borrow | Replaces the non-existent `user_deposit_and_borrow`. The borrow's scan reflects the newly registered collateral (`MarginIndexModel`). Atomic only when it fits in one transaction; otherwise the envelope reports dependencies and `atomic: false`. |
| `close_position` | 3 | farm redeem / reverse swap → repay(all) → re-read → close debt → withdraw(all) → close collateral → optional close margin | Multi-step; each step re-reads state |

### 3.6 `vanna_earn_market` — read

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `pool_stats` | 1 | `fetchPoolStates` | Projected to chain time: supplied, borrowed, available cash, utilisation, borrow APR, supply APR, exchange rate, reserve factor, caps, status |

### 3.7 `vanna_earn_position` — read

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `deposited_balance` | 1 | `fetchLenderPositions` | Shares and redeemable underlying |
| `earned_interest` | 1 | `computeEarnedInterest` | Interest earned to date, per Earn position |

### 3.8 `vanna_earn_write` — write

The existing `lender_tools.py` / `reserve.py` decode the obsolete four-field rate layout and must be rewritten on §2.8, and restructured into this dispatcher shape.

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `supply` | 2 | `lender_supply(assets, min_shares_out)` | Wraps SOL; first-supply minimum. **Verified against a live simulation** (2026-09-28): the account list has 13 accounts, not 12 — `token_program` (the underlying's) **and** `share_token_program` (always classic SPL, fixed by the IDL's own `address` constraint) are both present, separately, right before `associated_token_program`/`system_program`. Omitting `share_token_program` fails simulation with a `RequireKeysEqViolated` (Anchor custom error 3008) comparing the wrong accounts. |
| `redeem` | 2 | `lender_redeem(shares, min_assets_out)` | By shares, assets or `all`; checks available cash. Same correction: 11 accounts, ending `…, lender_share_account, token_program, share_token_program` — no `associated_token_program`/`system_program` (redeem doesn't create anything). |

`public_refresh_reserve` (permissionless accrual) is **not** an action — the
MCP accrues interest in memory for reads and only calls it internally if a
write needs a freshly-accrued reserve.

### 3.9 `vanna_farm_overview` — read (Kamino Lend, single-asset)

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `markets` | 1 | `FARM_CATALOGUE` + `/api/farm/pools` | Five vaults with Kamino live metrics (required, not optional — null only if the live call is transiently unavailable, never guessed) and integration `enabled` |
| `positions` | 1 | `farmPositionsFromSnapshot` | cToken holdings, underlying, USD |

### 3.10 `vanna_farm_kamino` — write

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `deposit` | 3 | `farmDeposit` | Price refresh; `margin_execute(KLend deposit_reserve_liquidity)` from the margin vault's balance at 1.4M CU. No wallet funding or borrow leg — the Farm UI supplies from the margin balance only |
| `redeem` | 3 | `farmWithdraw` | `margin_execute(KLend redeem_reserve_collateral)`; underlying returns to the margin vault |

Adapter rules: the first `cpi_account_count` remaining accounts are exactly the KLend call accounts (`adapters/kamino.rs` roles), the margin PDA is the owner, and the reserve must be the cToken's registered `price_source_account`. **Kamino Liquidity (multi-asset LP) is not supported and cannot be** — that program rejects the CPI (`CPINotAllowed`).

### 3.11 `vanna_swap` — read + write (Jupiter)

| Action | Type | Phase | Mirrors | Purpose |
|---|---|---|---|---|
| `quote` | read | 1 | `fetchJupiterQuote` | ExactIn quote; both sides must be discovered assets |
| `can_swap` | read (pre-flight) | 1 | Post-trade health | Fails closed. N/A for `wallet_swap`, which never touches a margin account. |
| `margin_swap` | write | 3 | `executeMarginSwap` | `/swap-instructions` with `userPublicKey = margin PDA`, `useSharedAccounts`, no SOL wrap; only `route` / `shared_accounts_route`; `margin_execute(min_received = otherAmountThreshold)`. This is the on-chain capability behind any future leveraged directional position (composed with `margin_trade.deposit`/`borrow` — see "Composite flows" below); it is **not** a shipped "Perps" product feature today. |
| `wallet_swap` | write | 3 | `executeWalletSwap` | Plain Jupiter swap from the wallet — the real `/trade/spot` product surface. Blockhash from the configured RPC; not a Vanna instruction. |

The Copilot never offers arbitrary program invocation; only these two adapters, and only when their `Integration` is enabled.

### 3.12 `vanna_history` — read

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `margin` | 1 | `fetchMarginHistory` | Event rows with post-action HF where reported, signature, time, `complete` flag |
| `earn` | 1 | `fetchEarnHistory` | Supply/withdraw rows |

Scans use the **owner wallet**.

### 3.13 `vanna_wallet` — read

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `summary` | 1 | `fetchWalletBalances` | Native SOL (spendable after 0.05 SOL), every catalogue token, stray wSOL, lender shares, margin account exists |
| `balance` | 1 | — | Wallet or margin-vault balance for one asset (explicit token program) |
| `token_metadata` | 1 | — | Decimals, supply, token program, transfer-fee extension if present |

`connect_status` (an auto-sign capability check) was removed: there is no auto-sign on Solana.

### 3.14 `vanna_sign` — read

Signing is the user's connected wallet, through the app's execution adapter (`vanna-credit-backend/docs/copilot/COPILOT_ARCHITECTURE.md` §4-§5): build → simulate → wallet signs → hosted-RPC broadcast → confirm. There is no Sign Service and no auto-sign, so the MCP only builds and reports status.

| Action | Phase | Mirrors | Purpose |
|---|---|---|---|
| `tx_status` | 1 | `getSignatureStatuses` | Confirmation status, confirmations, err |

The Stellar-style `sign_and_submit` and auto-sign session actions were removed. A remote HTTP signer would be a new, separate adapter, only if a later phase explicitly asks for one.

### 3.15 `vanna_fork` — dev only

| Action | Type | Purpose |
|---|---|---|
| `faucet` | dev cheatcode | `POST /api/faucet` `{ address, symbol? }` → `{ ok, results, note }` or `{ error }`. Registered only when `SOLANA_CLUSTER_LABEL` is a fork **and** `ENABLE_FORK_TOOLS=true`; never in the Copilot's allowlist. Not a devnet airdrop. |

### 3.16 `vanna_plan` — plans and multi-step actions (the Copilot's planner and workflow engine)

Added 2026-10-04 so an MCP client (Claude, Cursor) can do everything the website Copilot does. The client's model
decides what the user wants; this tool does every number and every rule. Mirrors `vanna-credit-backend`
`lib/copilot/{workflow/strategy,workflow/unwind,workflow/steps,workflow/funding,plan,risk}.ts`.

| Action | Purpose |
|---|---|
| `lever` | "lever X Nx": deposit X if not already margin collateral → borrow a stable (`borrow_asset`; omitted = the tool returns the choices) → `margin_swap` into X → report HF. Sized so the 1.15 floor holds with the swap at its worst allowed price; checks pool liquidity, `can_borrow`, a live quote, wallet funding |
| `unwind` | "close everything" (`scope` all/margin/earn/farm, `close_account`): Kamino redeem → repay each debt (margin balance, else wallet, else swap other collateral in by *required output*) → withdraw → Earn redeem; health only improves; capped at 8 steps, never cut between a swap and its repay |
| `swap_exact_out` | "swap SOL to get 100 USDC": input sized from live quotes (4 correction rounds) so the *minimum received* covers the target |
| `custom` | a chain of up to 8 steps (`create_account, earn_supply, earn_redeem, deposit, withdraw, borrow, repay, swap, farm_deposit, farm_redeem, close_account, report`); amounts exact / `all` / `NN%` (resolved once) / `out:N` |
| `status` / `cancel` | plan and per-step state (`pending, built, submitted, confirmed, skipped, failed, uncertain, expired`); owner-only |

A drawn plan is frozen (exact amounts, sha256 digest over every executable slot + wallet + floor), valid for approval for 5
minutes, runnable for 30. It returns `plan_url`: the user opens it, **approves by signing a message that names the digest**
with their own wallet, and signs each step. Each step is pre-flighted again against live data when its turn comes
(`mcp_server/planning/preflight.py`: "all"/percent resolution, the 1.15 gate on borrow/withdraw/margin swap, per-action
and daily USD caps, funding), then built and simulated by the existing write dispatchers; a step that would breach the
floor **pauses** the plan. Exactly-once per step; an unclear send is `uncertain` and never retried; a transaction that
never lands is rebuilt. The server checks the signed bytes equal its own build and relays them. Needs `ENABLE_SIGN_LINKS`;
without it a drawn plan is a preview (`executable: false`).

### 3.17 `vanna_risk` — advisor reads (the Copilot's capacity / what-if / liquidation answers)

| Action | Purpose |
|---|---|
| `capacity` | largest SAFE borrow or withdrawal at the floor, per asset, verified through the program's `can_*`; `protocol_max` (1.10) for reference |
| `what_if` | `op` borrow/withdraw/repay with an `amount` (program's own check, projected HF) or a `target_hf` (solves for the amount) |
| `max_leverage` | highest leverage on an amount that holds the floor at the swap's worst price, plus a 1.5x–10x table |
| `liquidation_distance` | % collateral can fall before HF reaches 1.10 (read-only; liquidation is never executed) |
| `funding_check` | can the wallet pay for a plan's wallet-funded steps |
| `policy` | the product's floor, limits, asset policy and refusals |

### 3.18 `vanna_portfolio` — the Portfolio page

| Action | Purpose |
|---|---|
| `overview` | wallet tokens with USD, margin account (collateral, debt, equity, leverage = gross collateral / equity, LTV, HF), Earn deposits with interest and APR, Kamino positions, net worth (receipts not double-counted) |

### 3.19 `vanna_perps` — Phoenix perps (2026-10-08)

| Action | Purpose |
|---|---|
| `markets` | SOL, ETH, BTC, TSLA, META: index / mark price, max leverage, open, taker fee |
| `positions` | the margin's Phoenix balance, withdrawable now, each position with PnL and liquidation prices |
| `preview` | an order's size, limit price, fee, HF and liquidation price after (nothing built) |
| `register` / `open` / `close` / `withdraw` | the first-trade trader account; open (margin USDC -> Phoenix, IOC order); reduce-only close; USDC back |

Perps never borrow from Vanna. Mirrors `lib/solana/perps.ts`.

Each of an open's three `margin_execute` calls (Ember wrap, Phoenix deposit, order) re-checks the margin's health, pricing
every asset it holds. On an account holding many assets they exceed one transaction's 1.4M compute units; then `open`
returns only the funding transaction (wrap + deposit) with `summary.prerequisite_kind = "phoenix_fund"`, and once it
lands the caller opens again with the same arguments and `funded=true`: the order is sized from that USDC, now in
Phoenix, and nothing more moves. The app's `openPerp` still builds the single transaction.

### 3.20 `vanna_vaults` — Basis and Perena vaults (2026-10-09)

| Action | Purpose |
|---|---|
| `list` | each vault's NAV, share price, deposits open, deposit limit, hedge (Basis); the app's APY with its source (`apy_*_source`: `history`, or `deposit_event_estimate` with `apy_*_baseline`, an approximate figure from past deposits since backend `fa6d2f6`) and `apy_history_status`; Perena's published APY; Phoenix funding for Basis; Basis `return_since_launch_pct` / `since_launch_days` / `launched_at` (raw share-price return since launch, never annualised, shown until a 7-day figure exists, backend `aaed5cc`) |
| `positions` | the wallet's vault shares in its margin account (and SOL Basis shares left in the wallet) with collateral value |
| `preview` | a simulated deposit (USDC) or withdrawal (shares): output, minimum, HF after (nothing built) |
| `deposit` / `withdraw` | `margin_execute` into Perena or the Basis vault program (Basis runs its own Jupiter route; min out = simulated - 0.5%) |
| `move_shares` | SOL Basis: deposit vbSOL left in the wallet (minted above a deposit's minimum) into the margin account |

Mirrors `lib/solana/vault-actions.ts`, `vault-state.ts`, `perena.ts` and `@vanna/phoenix-vault-client` 0.1.0.

- **SOL Basis deposit (backend `82c5a05`).** The deployed SOL vault can't take a deposit through `margin_execute`, so it
  is ONE transaction paid from the margin's USDC, like the app's `walletRouted` deposit: `user_withdraw_collateral`
  (margin USDC -> wallet; with debt, health-checked with every other position), the vault `deposit` from the wallet,
  then `user_deposit_collateral` of wallet vbSOL already held + the minimum minted. Atomic. The simulation reads the
  `CollateralWithdrawn` health factor (`health_factor_while_usdc_moves`: the lowest point, which the program checks);
  a `HealthFactorTooLow` refusal returns the app's wording ("…would push your health factor to or below 1.10…").
  vbSOL minted above the minimum (~0.5%) stays in the wallet: `move_shares`, or the next SOL Basis deposit sweeps it.
- **Market session.** A Basis vault whose `after_hours_deposits` is off (TSLA, META) refuses deposits while its Phoenix
  market is after hours (the perp asset map's session bits, backend `44aba70`). `deposit` checks it before building and
  answers with the app's wording; `list` shows `market_after_hours`, `deposits_open: false` and `deposit_blocked_reason`.
- **Route size.** A Basis deposit or withdrawal swaps through Jupiter inside the vault call. A larger amount can need a
  larger route; when the transaction passes 1232 bytes it is rebuilt with a tighter route cap (30, then 22, then 16
  accounts), and if it still doesn't fit the error says to use a smaller amount or split it.
- **Jupiter access.** Without a key of its own the MCP calls Jupiter through the app's `/api/jupiter` proxy (as the
  app's vault actions do), and retries a rate-limited (429) answer once.
- **App access gate.** The app answers every `/api/*` call without its `vanna_access` cookie with 401 (backend
  `aaed5cc`, `proxy.ts`). Every app call the MCP makes (price keeper, Phoenix helpers, Jupiter proxy, vault rows,
  faucet, Predict) enters `APP_ACCESS_CODE` once at `POST /api/access`, keeps the 30-day cookie per app origin, and
  logs in again once on a gate 401 (`vanna_core/app_gate.py`). Without the code a gated app's 401 comes back as is.

A Basis share is priced by simulating the oracle; when that read fails (a transient RPC error), the last good price from
the past hour is used, flagged not fresh, like the app (backend `039a46b`), so the shares don't drop to $0 in a health
reading. A health-checked write still refuses a price that isn't fresh (`oracle_reader._LAST_BASIS_PRICE`).

### 3.21 `vanna_predict` — Predict, World.xyz prediction markets (2026-10-12)

The app's Predict page (backend `1a9cb3c`, `aaed5cc`; `lib/predict/*`, `app/api/predict/*`), through the app's API:
the app's server builds every trade and its market maker co-signs it, so the MCP cannot build one itself.

| Action | Calls (the app) | Purpose |
|---|---|---|
| `markets` | `GET /events?cat=&q=` | listed events; each outcome's `market_id`, buy / sell price in cents (YES and NO), tradable, joins Vanna's pool |
| `market` | `GET /events/:id` | one event (by event id or any of its market ids) with pool figures and rules |
| `positions` | `GET /positions/:owner` | wallet CASH, positions (wallet + pooled shares, value, P&L, pays if win), Vanna pool, World shares it can move into the pool |
| `history` | `GET /activity/:owner?market=` | the wallet's trades on the fork |
| `quote` | `POST /trade` | the trade's quotes and warnings; the transactions are dropped (a read) |
| `trade` (write) | `POST /trade` | a batch envelope: buy (CASH, 0.1-5,000), sell (shares; pooled shares redeemed first), deposit (World shares into the pool); one leg, or `legs` (max 8) |
| `merge` (write) | `POST /pool/merge` | the app's keeper merges matched YES+NO into USDC lent to Vanna; called after a trade confirms (`summary.merge_markets`) |
| `faucet` (write) | `POST /faucet` | test fork + `ENABLE_TEST_FAUCET` only: test CASH (default 100, max 1000; the page's "Get test CASH") |

- **As on the website:** paid with CASH only (`pay: "USDC"` is not offered) and a buy on an allowlisted market always
  joins the pool (no `noPool`). Wallet-only: no margin account, no borrowing, no health factor.
- **The batch.** One trade is 1..n v0 transactions sharing one blockhash. `summary.batch`, `sign_all_at_once`,
  `send_in_order`: the wallet signs all of them in ONE approval (`signAllTransactions`), and they are sent in order,
  each confirmed before the next (a later one spends what the one before created). Only the first is simulated; the
  rest are `not_run_dependency`. Each keeps the maker's signature (`summary.co_signer`, `requiredSigners`).
- **Checked before it is returned:** every transaction is paid by the user's wallet; its only other signer is the
  app's maker (from `GET /status`, cached 10 minutes) and that signature is present; it calls only World, the outcome
  pool, SPL Token, Token-2022, associated-token, compute budget and system. Anything else is refused.
- **Limits:** a trade's spend for the per-action / per-day limits is its buys' CASH at $1 (`guard.predict_spend_usd`).
- **Sign page:** a batch step is signed with one `signAllTransactions` and submitted in order; the server sends each
  and waits for it to confirm before it accepts the next (`engine._wait_landed`), refuses an out-of-order or tampered
  transaction, verifies the maker's signature too (`signing.verify_signed_transaction(co_signers=…)`), never rebuilds a
  batch part of which landed, and runs the merge after the last one confirms. Direct writes with no plan op (vaults,
  perps, Predict) are `raw` plan steps, accepted by `validate_steps` (they were refused before, so they got no link).

### Extensions to existing dispatchers (2026-10-04)

- `vanna_wallet` `faucet` (write-scoped): tops up the caller's wallet on the test fork (needs `ENABLE_TEST_FAUCET` and a fork
  cluster; the app's faucet endpoint; capped per wallet per day). Replaces `vanna_fork` for users; `vanna_fork` stays dev-only.
- `vanna_earn_market` `rate_curve`: borrow and supply APR at every 5% utilization from the reserve's on-chain curve.
- `vanna_protocol_info` `concepts`: the Copilot's plain-language explanations (health factor, liquidation, leverage,
  collateral, earn, farm, margin).
- Every write dispatcher, when `SAFETY_LAYER` is on, is pre-flighted by `mcp_server/planning/guard.py` (same code as a plan
  step) and returns `sign_url`/`plan_id` when signing pages are enabled (a single action is a one-step plan).

Tool count is now **17 dispatchers** (14 original + `vanna_plan`, `vanna_risk`, `vanna_portfolio`). The Copilot's own
allowlist is unchanged: it keeps calling the original 14 by name.

### Composite flows (built from the dispatchers above, not separate actions)

`vanna_farm_kamino.deposit` supplies from the margin balance only; funding it
first is a separate `vanna_margin_trade.deposit` (and `borrow`, for a
lend/borrow asset). A hypothetical leveraged directional swap (deposit → borrow
→ `vanna_swap.margin_swap`) is the same pattern, but **has no product UI
today** (no Perps page, no leverage UI — confirmed against `vanna-credit-backend`:
`app/trade` only redirects to a plain wallet-swap page). Don't build or name
this as a "Perps" feature; if it's ever wanted, it's a documented capability
composed from `vanna_margin_trade` + `vanna_swap`, likely needing lookup-table
setup, not a new dispatcher.

### Not exposed as a Copilot-facing action anywhere

`public_liquidate` execution: **v1 execution disabled for the user Copilot.**
`vanna_margin_status.liquidation_snapshot` (read) ships; the write builder is
isolated behind `ENABLE_LIQUIDATION_BUILDER=false` and, if ever enabled, is a
separate keeper-only builder outside this dispatcher set — not a
`vanna_margin_trade` action. Debt and seized collateral must share one token
program; simulation is authoritative, the off-chain pre-check guarantees
nothing after state changes.

---

## 4. Action count and exclusions

| Dispatcher | Actions | Read / write | Phase(s) |
|---|---|---|---|
| `vanna_oracle` | 2 | read | 1 |
| `vanna_protocol_info` | 3 | read | 1 |
| `vanna_account` | 5 | 3 read, 2 write | 1–2 |
| `vanna_margin_status` | 7 | read | 1 |
| `vanna_margin_trade` | 10 | write | 2–3 |
| `vanna_earn_market` | 1 | read | 1 |
| `vanna_earn_position` | 2 | read | 1 |
| `vanna_earn_write` | 2 | write | 2 |
| `vanna_farm_overview` | 2 | read | 1 |
| `vanna_farm_kamino` | 2 | write | 3 |
| `vanna_swap` | 4 | 2 read, 2 write | 1, 3 |
| `vanna_history` | 2 | read | 1 |
| `vanna_wallet` | 3 | read | 1 |
| `vanna_sign` | 1 | read | 1 |
| `vanna_fork` (dev only) | 1 | write, dev-gated | any |

14 production dispatchers (46 actions, all built) + 1 dev-only. The
read/write split per action is enforced by `mcp_server/scopes.py`: callers
without write scope don't see the write-only dispatchers and are refused
write actions on mixed ones. Outside the dispatcher set entirely: the isolated,
off-by-default `public_liquidate` keeper builder.

**Never exposed in v1** (no admin wrappers): `initialize_protocol`, `admin_propose_authority`, `authority_accept_admin` (needs the pending admin's signature), `admin_set_operating_mode`, `admin_register_asset`, `admin_update_asset_config`, `admin_set_asset_price_source`, `admin_initialize_reserve`, `admin_update_reserve_config`, `admin_collect_protocol_fees`, `admin_register_integration`, `admin_set_integration_enabled`. If a separate admin tool is ever built, it uses the IDL's exact accounts and signer checks and lives outside this server.

---

## 5. Code in this repo that has to change

> Historical: this is the original change list, all of it now done.
> Since then the Hermes settings and the `pyth_pull` adapter stub were dropped,
> and the current settings are listed in `.env.example`.

| File | Change |
|---|---|
| `idl/vanna_lending.json` | Replace the old Backend-Solana copy with `vanna-credit-backend/idl/vanna_lending.json` (identical to the layer's generated IDL); add `.gitattributes` `idl/*.json text eol=lf`; pin SHA-256 `b438bfdd…32d4` (LF) in a startup check |
| `vanna_core/anchor_codec.py` | Keep (anchorpy can't parse this IDL format); re-verify every discriminator against the new IDL |
| `vanna_core/config.py` | Env: `SOLANA_RPC_URL`, `SOLANA_WS_URL`, `SOLANA_CLUSTER_LABEL`, `VANNA_PROGRAM_ID` (default `H37YVBG3…`), `ORACLE_MODE`, `PRICE_KEEPER_URL`, `PYTH_HERMES_URL`, `PYTH_API_KEY`, `JUPITER_API_KEY`, `JUPITER_FORK_DEXES`, `ENABLE_FORK_TOOLS`, `ENABLE_LIQUIDATION_BUILDER`; display catalogue (§1.2) |
| `vanna_core/layouts.py` → IDL coder | Decode all six accounts with the IDL (Anchor coder) and validate owner, discriminator and PDA; `MarginAccount` `[u16; 24]` sparse; `Reserve.rate_curve` three WAD coefficients |
| New `vanna_core/assets.py` | §1.3 discovery and `unsupported_asset` |
| `vanna_core/math_utils.py` | Port contract math (§2.4, §2.8) with exact rounding; `math.ts` is the cross-check |
| `vanna_core/pdas.py` | `integration_pda`, `price_feed_account(feed_id)` (shard 0), ATAs with explicit token program and `allowOwnerOffCurve` |
| `vanna_core/client.py` | Unsigned v0 compile, compute budget, 1232-byte check → lookup-table setup + `not_run_dependency`, simulation, envelope v1 (§2.1). **No send, sign or confirm code paths.** |
| New `vanna_core/margin_model.py` | `MarginIndexModel` + scan builder with §2.5 exclusions and hard errors |
| New `vanna_core/wsol.py` | Wrap/unwrap and the seeded temporary receiver |
| New `vanna_core/oracle/` | `base.py` interface, `dev_fork.py`, `pyth_pull.py` (stub until credentials), shared `validate.py` |
| New `vanna_core/history.py`, `errors.py` | Event decoding; IDL error map + Jupiter/stale-pool classifiers |
| New `vanna_core/envelope.py` | Schema v1 types, decimal-string integers, `signing_status` mapping |
| `mcp_server/tools/lender_tools.py` | Restructure into the `vanna_earn_market` / `vanna_earn_position` / `vanna_earn_write` dispatcher shape (§3.6–3.8) — one tool per dispatcher with an `action` parameter, not 5 flat functions |
| New `mcp_server/dispatchers/` (or equivalent module per dispatcher) | One module per §3 dispatcher (`oracle.py`, `protocol_info.py`, `account.py`, `margin_status.py`, `margin_trade.py`, `earn_market.py`, `earn_position.py`, `earn_write.py`, `farm_overview.py`, `farm_kamino.py`, `swap.py`, `history.py`, `wallet.py`, `sign.py`, `fork.py`), each registering one FastMCP tool with an `action` enum and dispatching to `vanna_core` |
| Tests | See §7.3 |

---

## 6. Question log

### 6.1 Resolved (from the answers PDF)

| # | Question | Resolution |
|---|---|---|
| 1 | Target network | Surfpool mainnet fork for development (hosted by default in the backend, local in scripts). Launch cluster still Pending (§6.2). |
| 2 | Who builds transactions | **The MCP**, from the IDL, as unsigned builders. Backend services are references; their instruction construction is ported, their send paths never called. |
| 3 | Signing interface | No service exists. Envelope v1 + execution-adapter response adopted. Phase 1 execution lives in the app (wallet signs, app broadcasts). |
| 5 | Liquidation scope | v1: reads only; keeper builder isolated and off |
| 6 | Liquidation threshold | 1.10 global, equality liquidatable; 50% close factor; 1.15 is UI policy |
| 7 | Admin scope | No admin wrappers; read-only config |
| 8 | Composite size | Lookup-table setup as a confirmed dependency, then rebuild (§2.1) |
| 9 | Program ID | `H37YVBG3…` everywhere in the working tree; README `BZ812…` stale |
| — | Faucet | Fork-only dev tool, disabled in production |
| — | Swap/farm timing | After core lending/margin (phase 3); receipt-aware reads from day one |

### 6.2 Team decisions of 2026-09-29 (these were the pending items)

All six are answered.

1. **Cluster / IDL** — the hosted fork; the live executable matches the team's pinned build and the backend IDL equals the build IDL (D1). That build is layer HEAD `ecdb9b6` (D8).
2. **Asset list** — SOL/USDC/USDT lend + borrow; JitoSOL/JupSOL collateral, swap, farm; JupSOL priced by the keeper from Jupiter on the fork (D2).
3. **Execution** — the connected wallet signs and pays fees (incl. lookup-table setup); no Sign Service, no Bearer-auth signing endpoint (D3).
4. **Hermes** — not used this release; the keeper is the pricing mechanism (D4).
5. **Liquidation** — `public_liquidate` exists on-chain but has no user-facing wrapper or UI; read-only in the MCP (D5).
6. **Swap/farm** — implemented and UI-connected; not "pending" (D6). Admin: no wrappers; faucet: fork-only dev tool.

---

## 7. Implementation readiness

### 7.1 Phases

| Phase | Scope | Depends on |
|---|---|---|
| 1 | IDL pin, discovery, PDAs, all read tools, exact health/interest math, `tx_status`, history | Nothing pending |
| 2 | Core write builders (account, collateral, borrow, repay, Earn, `deposit_and_borrow`), envelope v1, lookup-table dependency path, keeper price refresh | Nothing pending |
| 3 | `margin_execute` builders: Kamino farm, margin/wallet swap, close position | Nothing pending (swap/farm are live in the app) |
| 4 | Hosting: OAuth resource server, write-scope gating, rate limit, read cache, Docker/Cloud Build | Deploy needs the login provider and hosting project (both still open) |

### 7.2 Hard rules for every write tool

1. Build from the pinned IDL; never call a send/sign path.
2. Re-derive and validate every account read (owner, discriminator, PDA).
3. Complete scan or fail (`unsupported_asset`, `price_unavailable`, `stale_price`).
4. Simulate; report `not_run_dependency` honestly.
5. Integers as decimal strings; no private keys in output.

### 7.3 Required tests (from the answers PDF, plus parity)

- IDL hash snapshot; startup refusal on mismatch.
- Empty, partial and sparse margin arrays; unknown asset index → `unsupported_asset`.
- Debt rounding (up) and collateral rounding (down); **HF exactly 1.10 = liquidatable**; zero debt → `noDebt`.
- Kamino receipts in health; stale prices fail closed.
- Composite deposit + borrow with newly active collateral (scan reflects it) and simulation.
- Expired blockhash → rebuild; lookup-table dependency → `not_run_dependency` then rebuild.
- Parity with the backend's `scripts/e2e-fork.ts` and `scripts/e2e-exec.ts`: for the same inputs, identical instructions.
- Contract suites to mirror: `test_math`, `test_full_flow`, `test_liquidation`, `test_security`, Kamino/Jupiter externals.
