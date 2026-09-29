# Vanna Solana MCP — Build Plan

This is the execution plan for building this repo into the MCP server
specified in `TOOLS_CATALOGUE.md`. That file is the single source of truth
for every PDA, formula, gate, and dispatcher/action name — if this plan and
the catalogue ever disagree, the catalogue wins and this plan gets corrected,
not the other way round. Project decisions (cluster, assets, signing,
pricing, liquidation, swap/farm) and the open items are in `DECISIONS.md`.

## Principles

1. **Foundation once, correctly.** PDAs, account layouts, math, and the write
   envelope are built one time in `vanna_core` and never re-derived per
   dispatcher. Every dispatcher is a thin MCP-layer wrapper over `vanna_core`.
2. **One dispatcher is "done" when three things exist**: the `vanna_core`
   read/write logic, the `mcp_server` dispatcher tool (action-routed, per
   `TOOLS_CATALOGUE.md` §3's shape), and a test — offline where the logic is
   pure (math, layout decode, PDA derivation), live-fork where it must read
   real accounts. No dispatcher ships untested.
3. **No shortcuts on the non-negotiables**: no tool ever signs or broadcasts;
   no admin instruction is ever wrapped; liquidation execution never appears
   in the Copilot-facing dispatcher set (read-only `liquidation_snapshot`
   only); every integer is a decimal string end-to-end, never a float, in any
   money or health-factor value.
4. **Build order follows dependency order, not catalogue order.** Foundation
   → simplest reads → reads other reads depend on → writes → external
   integrations → production hardening. The phase numbers below match
   `TOOLS_CATALOGUE.md` §7.1.

---

## Phase 0 — Foundation (blocks every dispatcher)

Done in this order because each step depends on the one before it:

| # | File | What changes |
|---|---|---|
| 1 | `idl/vanna_lending.json` | Replace with the verified 27-instruction IDL from `vanna-credit-backend/idl/vanna_lending.json` (content-identical to `vanna-credit-layer`'s generated IDL; SHA-256 `b438bfdd…` with LF line endings, `2125e784…` with CRLF — either is fine, the hash check normalizes line endings) |
| 2 | `vanna_core/config.py` | Program ID → `H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC`; asset table → the real 5 assets + 5 Kamino receipts; add `ORACLE_MODE`, `PRICE_KEEPER_URL`, `JUPITER_API_KEY`, `ENABLE_FORK_TOOLS`, `ENABLE_LIQUIDATION_BUILDER`; RPC default → the hosted Surfpool fork. (Hermes settings were planned here and later dropped, `DECISIONS.md` D4.) |
| 3 | `vanna_core/pdas.py` | Add `integration_pda`, `price_feed_account` (shard-0 + feed id), and an ATA helper that takes an explicit token program |
| 4 | `vanna_core/layouts.py` | Rewrite all 6 account structs (`ProtocolConfig`, `AssetConfig`, `Reserve`, `MarginAccount`, `DebtPosition`, `Integration`) plus `RateCurve`/`PriceSource`/`AdapterKind`, straight from the real IDL's `types` section — 24-slot margin arrays, polynomial rate curve, `price_source`/`price_source_account`/`price_source_program` on `AssetConfig` |
| 5 | `vanna_core/assets.py` (new) | The display catalogue (§1.2) + runtime `AssetConfig`/`Reserve` discovery (§1.3): `asset_index → AssetConfig → mint`, validated by owner/discriminator/PDA, never silently dropping an unknown active index |
| 6 | `vanna_core/math_utils.py` | Rewrite: health formula (§2.4, hardcoded `1.10` WAD), the polynomial rate curve and supply/borrow APR (§2.8), share-price/redeem math (§2.8) — `Decimal`/`int` only, decimal-string output, never a float |
| 7 | `vanna_core/types.py` | Update models to match: `PoolStats` gets `RateCurve` fields instead of base/slope/kink; `MarginHealth` reports a decimal string or an explicit "no debt" sentinel, never `float` |
| 8 | `vanna_core/envelope.py` (new) | Schema v1 types (§2.1): `schemaVersion`, `cluster`, `programId`, `feePayer`, `transactions[]` (`id`, `dependsOn`, `format`, `encoding`, `unsignedTransaction`, `blockhash`, `lastValidBlockHeight`, `requiredSigners`, `simulation`), `summary`, `preview`, `signing_status`, `code`, `reason`, `simulation_success` |
| 9 | `vanna_core/client.py` | Build v0 transactions and wrap them in the schema-v1 envelope. Single-transaction case first (covers every Phase 1–2 action); the multi-transaction `dependsOn`/lookup-table path is added in Phase 3 when Kamino/Jupiter actually need it — the envelope shape doesn't change, only whether `transactions` has one entry or several |
| — | `vanna_core/anchor_codec.py` | **No change** — its discriminator formula was verified byte-for-byte against the real IDL's declared discriminators already |

**Definition of done:** an offline test suite (`tests/test_foundation.py`, no
RPC) passes — layout round-trips against hand-built byte strings, math
formulas match hand-computed examples from the PDF's worked numbers, PDA
derivations match the known addresses in `TOOLS_CATALOGUE.md` §1.

---

## Phase 1 — Read dispatchers

Build order (each item lists what it needs from the row above it):

1. **`vanna_protocol_info`** (`config`/`assets`/`integrations`) — simplest, exercises `assets.py` discovery with no math
2. **`vanna_oracle`** (`price`/`prices`) — needed by every health-checked read after this
3. **`vanna_earn_market` + `vanna_earn_position`** (`pool_stats`/`deposited_balance`/`earned_interest`) — `contracts/reserve.py` already exists and mostly just needs its math/layout calls fixed to Phase 0's rewrite, then reshaped into the dispatcher pattern
4. **`vanna_account`** (`resolve`/`get`/`health`) — needs a new `margin_model.py` (slot scan + health calc, §2.4/§2.5)
5. **`vanna_margin_status`** (`collateral`/`debt`/`can_borrow`/`can_withdraw`/`liquidation_snapshot`) — built on `margin_model.py` and `vanna_oracle`
6. **`vanna_wallet`** (`summary`/`balance`/`token_metadata`; `connect_status` was later removed, `DECISIONS.md` D3)
7. **`vanna_history`** (`margin`/`earn`) — Anchor event decoding, no indexer
8. **`vanna_farm_overview`** (`markets`/`positions`) — Kamino receipt exchange-rate valuation
9. **`vanna_swap`** read half only (`quote`/`can_swap`) — the write half (`margin_swap`/`wallet_swap`) is Phase 3
10. **`vanna_sign`** read half only (`tx_status`)

## Phase 2 — Core write dispatchers

1. **`vanna_earn_write`** (`supply`/`redeem`) — finish the SOL-wrap / wallet-ATA prefix `reserve.py` is still missing
2. **`vanna_account`** write half (`create`/`close`)
3. **`vanna_margin_trade`** (`deposit`/`withdraw`/`borrow`/`repay`/`repay_from_wallet`/`deposit_and_borrow`/`close_position`) — the biggest dispatcher: needs the §2.5 `remaining_accounts` scan and the `dev-fork` oracle adapter wired into every health-checked action

## Phase 3 — Integrations

1. **`vanna_farm_kamino`** (`deposit`/`redeem`) — Kamino adapter, 12 CPI accounts, 1.4M CU
2. **`vanna_swap`** write half (`margin_swap`/`wallet_swap`) — Jupiter adapter, v0 + lookup tables, the multi-transaction envelope path lands here

## Phase 4 — Hosting

Re-scoped by the 2026-09-29 decisions (`DECISIONS.md`): there is no Sign
Service (the wallet signs, D3) and no Hermes adapter (the keeper prices the
fork, D4), so phase 4 is the hosting layer that lets the Copilot call this
MCP over HTTP, ported from the Stellar MCP:

1. OAuth 2.1 resource server (`auth.py`, `identity.py`), write-scope gating per action (`scopes.py`, `main.py`)
2. Token-bucket rate limit and circuit breaker (`rate_limit.py`)
3. Short-TTL read cache for shared market data (`read_cache.py`)
4. Keeper price refresh as the default (`oracle_refresh.py`, D4)
5. `Dockerfile`, `cloudbuild.mcp.yaml`; deploy waits on the login provider and hosting project (O2, O3)

`vanna_fork` (dev-only `faucet`) can be built any time after Phase 0; it's low
priority and gated behind `ENABLE_FORK_TOOLS`.

---

## Testing strategy

- **Offline unit tests** (no RPC, always run): math formulas against hand-computed examples, layout decode/encode round-trips, PDA derivation against known addresses. Required for every Phase 0 file and every pure-logic piece of every dispatcher.
- **Live-fork integration tests** (network to the hosted Surfpool fork): read dispatchers against real accounts, write dispatchers up to and including simulation (never signing/broadcasting). Skip gracefully — don't fail the suite — if the fork RPC is unreachable from wherever tests run, same pattern the existing `funded_devnet_pubkey` fixture already uses.
- No test, anywhere, ever signs or submits a transaction.

## Status tracker

Checked off as each piece lands; update this table in the same commit/turn
that finishes the work, not after.

| Phase | Item | Status |
|---|---|---|
| 0 | idl/vanna_lending.json | ✅ replaced, address/hash verified live; later regenerated from layer `ecdb9b6` source (sha `47cd9ced…`, see the port row and D8) |
| 0 | config.py | ✅ program ID + oracle/feature-gate settings rewritten |
| 0 | pdas.py | ✅ `integration_pda`/`price_feed_account`/ATA-with-token-program added |
| 0 | layouts.py | ✅ rewritten, byte-size verified against live accounts (372/258/236) |
| 0 | assets.py | ✅ real 5-asset + 5-receipt registry, `AssetRegistry` discovery skeleton |
| 0 | math_utils.py | ✅ rewritten from `interest.rs`/`shares.rs`/`health.rs` source, verified against live reserve numbers |
| 0 | types.py | ✅ `PoolStats`/`MarginHealth` updated to match |
| 0 | envelope.py | ✅ schema v1, single-transaction helper built |
| 0 | client.py (v0 envelope) | ✅ real v0 transaction + placeholder-signature simulation, chain-clock read |
| 0 | offline foundation tests | ✅ `tests/test_foundation.py`, 20 tests, all passing |
| 1 | vanna_protocol_info | ✅ built + live-verified (config/assets/integrations, all 10 registry assets) |
| 1 | vanna_oracle | ✅ built + live-verified (price/prices); Kamino-receipt pricing correctly deferred to vanna_farm_overview |
| 1 | vanna_earn_market / vanna_earn_position | ✅ built + live-verified (pool_stats, deposited_balance); `earned_interest` stubbed pending `vanna_history` |
| 1 | vanna_account (read) | ✅ built + live-verified (resolve/get/health) against a real on-chain position with real collateral+debt |
| 1 | vanna_margin_status | ✅ built + live-verified (collateral/debt/can_borrow/can_withdraw/liquidation_snapshot) |
| 1 | vanna_wallet | ✅ built + live-verified (balance, token metadata, per-asset wallet+lender summary, margin existence); `connect_status` removed 2026-09-29 (no auto-sign, D3) |
| 1 | vanna_history | ✅ built + live-verified (event-log decode of `getSignaturesForAddress`/`getTransaction`, margin + earn history) |
| 1 | vanna_farm_overview | ✅ built + live-verified (`list_markets` with decimal-corrected exchange rate, `get_positions`) |
| 1 | vanna_swap (read) | ✅ `quote`/`can_swap` built + live-verified against real Jupiter quotes and a real margin snapshot |
| 1 | vanna_sign (tx_status) | ✅ built + live-verified (`get_signature_status`/`get_transaction_logs`) |
| 2 | vanna_earn_write | ✅ `supply`/`redeem` built, SOL-wrap prefix included, live-simulated successfully on the fork |
| 2 | vanna_account (write) | ✅ `create`/`close` built + live-submitted successfully (real margin account created on-chain) |
| 2 | vanna_margin_trade | ✅ all 10 actions built and live-submitted end-to-end on-chain: `deposit`/`withdraw`/`close_collateral`/`open_debt`/`borrow`/`close_debt`/`repay`/`repay_from_wallet`/`deposit_and_borrow`/`close_position`. `deposit_and_borrow` proven atomic (single tx, projects the post-deposit active-slot state locally for the bundled borrow's `remaining_accounts` scan); `close_position` proven as a full unwind loop (repay→close_debt→withdraw→close_collateral→close_margin→done) including the `needs_wallet_topup` fallback for interest-accrual dust the margin vault alone can't cover (see bug notes below) |
| 3 | vanna_farm_kamino | ✅ `deposit`/`redeem` built + live-verified against Kamino's live main-market Reserve (both bugs below fixed) |
| 3 | vanna_swap (write) | ✅ `margin_swap` (via `margin_execute` CPI forwarding) / `wallet_swap` (Jupiter's own tx rebuilt with a fresh blockhash) built + live-submitted; known external Surfpool-fork DEX-pool drift documented, tests retry-tolerant |
| 4 | vanna_sign (write) | Removed by decision (D3) — the wallet signs; `vanna_sign` is `tx_status` only and the phase-4 actions return `invalid_input` |
| 4 | pyth-pull adapter | Not in scope (D4) — the keeper prices the fork; `pyth-pull` = no refresh; Hermes settings removed |
| 4 | cluster + IDL pin | ✅ target is the hosted fork (D1); IDL SHA-256 pinned, MCP refuses to start on mismatch; program version = layer `ecdb9b6` (D8). ⚠ the fork is currently empty and must be redeployed + bootstrapped |
| 4 | lend/borrow asset policy | ✅ SOL/USDC/USDT only (D2); JitoSOL/JupSOL rejected with `asset_not_offered` on supply/borrow paths |
| 4 | hosting layer | ✅ offline — `auth.py`, `identity.py`, `scopes.py`, `rate_limit.py`, `agent_firewall.py`, `main.py` (`ScopedMCPServer`, `/healthz`, discovery routes), Docker + Cloud Build; 27 tests in `tests/test_hosting.py`. Deploy waits on O2/O3 |
| 4 | read cache | ✅ offline — `read_cache.py` (30 s TTL, per-key lock, error results never cached) on protocol config/assets/integrations, oracle prices, pool stats, farm markets; `tests/test_read_cache.py` |
| 4 | keeper price refresh | ✅ offline — `PRICE_REFRESH_SOURCE=keeper` (default) calls the app's `POST /api/prices/refresh`, 60 s throttle, shared in-flight call; `tests/test_price_keeper.py`. Live check pending (see the D4 knock-on in D8) |
| — | vanna_fork (dev-only) | ✅ `faucet` built, gated behind `ENABLE_FORK_TOOLS` |
| — | oracle-refresh (`cheatcode` source) | ✅ the MCP's own `surfnet_setAccount` writer (fabricates Pyth accounts, restamps Scope); now opt-in via `PRICE_REFRESH_SOURCE=cheatcode`, never alongside the keeper |
| — | port to layer `ecdb9b6` | ✅ offline — new `AssetConfig`/`OracleConfig` layout, `pricing.py` oracle facade, `remaining_accounts.py` health tail without price accounts, whole-account `liquidation_snapshot`, `max_borrow`/`max_withdraw`, regenerated IDL (sha `47cd9ced…`). Owner confirmed this is the version the fork runs (D8). ⚠ **not verified live** — the fork was down (HTTP 502) after the port and came back empty |

**Second bug found and fixed (2026-09-28, live end-to-end testing):**
`DiscoveredAsset.config_address` was hardcoded to `""` in `assets.py` — every
margin-trade write that referenced an asset's `AssetConfig` address would
have failed with an empty-string `Pubkey.from_string` crash the moment a
non-Earn write path was built. Caught before it shipped, not via a live
failure, because a full live create→deposit→borrow→withdraw→repay flow was
signed and submitted for real (not just simulated) as part of building the
`live_margin_position` test fixture — this is now the standard for every
write dispatcher: build a real position, not just simulate one transaction
in isolation.

**Testing pattern established:** `tests/conftest.py`'s `live_margin_position`
fixture creates one real on-chain margin account (1 SOL collateral, 1 USDC
borrowed) once per test session, signed with a disposable keypair via
`tests/_live_helpers.py` — test-only, the MCP dispatchers themselves never
sign. Every subsequent read/write dispatcher test that needs a real,
non-empty position reuses this fixture instead of re-deriving fixture state.

**Bug found and fixed during the Earn build (2026-09-28):** `lender_supply`/
`lender_redeem`'s real account lists have a `share_token_program` account
(always classic SPL, fixed by the IDL) that TOOLS_CATALOGUE.md's original
account-list notes omitted. Caught by live simulation (`RequireKeysEqViolated`),
fixed in both `reserve.py` and the catalogue. Lesson for every remaining
dispatcher: verify every account list against a live simulation before
calling a contract wrapper done, not just against the IDL's summarized name
list — a name list doesn't show which accounts silently share a token
program.

**All 14 dispatchers (+1 dev-only) are now built and live-tested end-to-end
(2026-09-29).** Every action across every dispatcher has been built,
simulated, and (for writes) actually signed and submitted on the fork at
least once as part of test coverage. Full suite: 128 tests passing
(`./.venv/Scripts/python.exe -m pytest tests/ -q`). That run was against the
program *before* the `ecdb9b6` port; since the port only the offline tests
have run (83 pass), and the live suite is the first step once the fork is up
(`DECISIONS.md` "Gate for Copilot building").

**`deposit_and_borrow`/`close_position` composites — bugs found and fixed
during live end-to-end testing (2026-09-29):**
1. `close_position` referenced `MarginContract` without importing it —
   `NameError` on the very first call. Fixed with a local import matching
   the pattern already used elsewhere in the file.
2. `user_repay_from_margin(repay_all=true)` caps the transfer at
   `margin_vault.amount` (`account_manager/borrow.rs:395`). A margin vault
   that only ever held the exact principal borrowed can never cover
   `debt_shares_to_assets_up`'s rounding-up plus any interest accrued since
   the borrow, so a naive repeat of `repay_all` drains the vault to 0 then
   fails closed with `ZeroAmount` (6040) forever after. Matches
   `vanna-credit-backend/lib/solana/margin.ts`'s own pre-flight check
   (`REPAY_ALL_BUFFER_BPS = 20`, 0.2%) — `close_position` now checks the
   margin vault balance against `debt + 0.2% + 1` before building the repay
   batch, and if any asset falls short, returns a `needs_wallet_topup` step
   (asset, shortfall amount) instead of submitting a doomed transaction. The
   caller tops up via the existing `repay_from_wallet` action, then calls
   `close_position` again.
3. `withdraw_collateral` had no idempotent-create for the caller's own
   wallet-side ATA — withdrawing an asset the wallet has never held before
   failed closed with `AccountNotInitialized` (3012). Fixed by prepending
   `wallet_prep_instructions` (the same pattern `deposit_collateral` already
   used), matching TOOLS_CATALOGUE.md §2.2.3's rule that the Vanna program
   only ever auto-creates its own PDAs' token accounts, never the caller's.
4. `close_position` called again after `close_margin` already landed (the
   account's rent reclaimed) raised `AccountNotFoundError` instead of
   reporting the terminal `"done"` step — broke the "call repeatedly until
   done" contract. Fixed by checking `client.account_exists()` first and
   returning `"done"` immediately when the margin account no longer exists.

Verified live end-to-end after all four fixes: create → `deposit_and_borrow`
(atomic, 1 tx) → `wallet_swap` for top-up funds → `close_position` looped
through `needs_wallet_topup` → `withdraw_collateral` ×2 → `close_margin` →
`done`, then confirmed idempotent (`done` again on a further call). Covered
by `tests/test_margin_composites.py`.

Along the way, `mcp_server/error_handling.py` gained a `transaction_too_large`
error code for the (separately known, `vanna-credit-backend/lib/solana/tx.ts`
`isTooLarge`) case where a Jupiter route with enough hops compiles a v0
message over Solana's 1232-byte packet limit before it reaches simulation —
previously this surfaced as an opaque `internal_error`. A temporary-ALT
fallback (the backend's own solution) is not built here; a caller retries
with a smaller amount or a fresh quote instead.
