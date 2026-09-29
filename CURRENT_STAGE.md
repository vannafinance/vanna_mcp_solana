# Current Stage

Snapshot of what's built, what's deliberately out of scope, and how to test
it. This file tracks the *current state* of the build — see `BUILD_PLAN.md`
for the phase-by-phase history and every bug found along the way, and
`DECISIONS.md` for every project decision and open item.

## Where things stand (2026-09-29)

- All 14 dispatcher tools are built. Before the layer's 2026-09-29 program
  update they were live-tested against the hosted fork: real accounts
  created, real transactions signed and submitted with a disposable keypair,
  128 tests.
- The code has since been **ported to layer HEAD `ecdb9b6`** (new
  `AssetConfig`/`OracleConfig`, Scope-first pricing, no price accounts on
  borrow/withdraw/`margin_execute`, whole-account liquidation). The port is
  **verified offline only**. The fork returned HTTP 502 for hours after the
  port; when it came back it had been restarted **empty**, with no Vanna
  program, `ProtocolConfig` or assets (`handoff/program_probe.py`). So no live
  test has run on the port, and none can until the team redeploys and
  bootstraps.
- The owner confirmed the fork will run `ecdb9b6` (`DECISIONS.md` D8), so
  the port stays; the backend's committed IDL lags and isn't a layout
  reference.
- The team's 2026-09-29 decisions are applied in code: wallet signing only,
  keeper pricing, lend/borrow asset policy. The Stellar hosting layer is
  ported (auth, write-scope gating, rate limit, read cache, Docker).
- **Offline tests: 83 pass**, listed under "Testing" below.

## What's covered

| Tool | Actions | Notes |
|---|---|---|
| `vanna_protocol_info` | `config`, `assets`, `integrations` | Protocol config, every registered asset (discovered on-chain, with `lend_borrow_offered`), integration list |
| `vanna_oracle` | `price`, `prices` | Prices through the program's own oracle rules (`pricing.py`: Scope, Pyth fallback, Kamino receipt rate), with freshness/TWAP/confidence flags |
| `vanna_earn_market` | `pool_stats` | Utilization, APR, exchange rate — projected to the current chain clock |
| `vanna_earn_position` | `deposited_balance`, `earned_interest` | `earned_interest` is best-effort, bounded by `vanna_history`'s scan limit |
| `vanna_earn_write` | `supply`, `redeem` | Lender deposit/withdraw, with idempotent wSOL wrap for native SOL |
| `vanna_account` | `resolve`, `get`, `health`, `create`, `close` | Margin account lifecycle + full collateral/debt/health read |
| `vanna_margin_status` | `collateral`, `debt`, `can_borrow`, `can_withdraw`, `max_borrow`, `max_withdraw`, `liquidation_snapshot` | `liquidation_snapshot` is an eligibility **read** only — see "Not covered" |
| `vanna_margin_trade` | `deposit`, `withdraw`, `close_collateral`, `open_debt`, `borrow`, `close_debt`, `repay`, `repay_from_wallet`, `deposit_and_borrow`, `close_position` | The last two are composites — see below |
| `vanna_wallet` | `summary`, `balance`, `token_metadata` | `connect_status` removed — there is no auto-sign (D3) |
| `vanna_history` | `margin`, `earn` | Decodes Anchor events from `getSignaturesForAddress`/`getTransaction` logs |
| `vanna_farm_overview` | `markets`, `positions` | Kamino main-market receipt exchange rates and metrics |
| `vanna_farm_kamino` | `deposit`, `redeem` | Kamino single-asset supply/withdraw via `margin_execute`, from the margin balance (no wallet funding, no leverage — D6) |
| `vanna_swap` | `quote`, `can_swap`, `margin_swap`, `wallet_swap` | Jupiter v6; `margin_swap` uses `margin_execute` CPI forwarding, `wallet_swap` rebuilds Jupiter's own tx with a fresh blockhash |
| `vanna_sign` | `tx_status` | The only action. The connected wallet signs (D3); any other action returns `invalid_input` |
| `vanna_fork` *(dev-only, `ENABLE_FORK_TOOLS=true`)* | `faucet` | Proxies the backend's dev faucet; gated off by default |

### The two composites

- **`deposit_and_borrow`** — deposits collateral and borrows in **one atomic
  transaction**: it locally projects the margin account's active-slot state
  as it will be immediately after the deposit lands, then builds the borrow's
  `remaining_accounts` scan against that projection.
- **`close_position`** — a **repeatedly-callable** full unwind. Call it, sign
  and submit whatever it returns, call it again — it re-reads real on-chain
  state every time and returns the single next step:
  `repay` → (`needs_wallet_topup` if the margin vault can't cover debt +
  ~0.2% accrual dust — top up via `repay_from_wallet`, then call again) →
  `close_debt` → `withdraw_collateral` (one position at a time) →
  `close_collateral` → `close_margin` → `done`. Calling it again after `done`
  keeps returning `done` — it's idempotent, not an error.

### Oracle freshness (D4)

Every health-checked write (`borrow`, `withdraw_collateral`, the composites,
`margin_swap`, `farm_kamino.deposit/redeem`) refreshes fork prices before
building, so a stale price doesn't need a separate manual step.

- Default `PRICE_REFRESH_SOURCE=keeper`: calls the app's
  `POST {PRICE_KEEPER_URL}/api/prices/refresh` (Jupiter USD prices written to
  the fork's oracle accounts, Kamino reserves healed), at most once per 60 s,
  with concurrent callers sharing one call.
- `PRICE_REFRESH_SOURCE=cheatcode`: the MCP's own `surfnet_setAccount` writer,
  opt-in and never alongside the keeper.
- A failed refresh is logged and the build continues; a still-stale price
  fails closed in simulation.
- `ORACLE_MODE=pyth-pull` means no refresh. There is no Hermes adapter.

### Hosting layer (ported from the Stellar MCP)

`MCP_TRANSPORT=streamable-http` serves `POST /mcp` plus `/healthz` and the
OAuth discovery routes.

- `AUTH_ENABLED=true` makes it an OAuth 2.1 resource server (JWKS-verified
  bearer).
- Callers without write scope don't see `vanna_margin_trade`,
  `vanna_earn_write`, `vanna_farm_kamino` or `vanna_fork`, and are refused the
  write actions of mixed dispatchers (`insufficient_scope`).
- A token-bucket rate limit (write 10, heavy read 5, read 1) and a circuit
  breaker protect the RPC.
- Shared market reads are cached for 30 s.
- `Dockerfile` and `cloudbuild.mcp.yaml` are ready. Deploy waits on O2/O3.

## Product-scope decisions (confirmed by the team)

Summary only; the register with sources and code consequences is `DECISIONS.md`.

- **Cluster:** the hosted Surfpool mainnet fork (`https://rpc-devnet.solana.vanna.finance`)
  is the current target. The deployed program binary matches the pinned local
  build, and the backend IDL equals the build IDL. The MCP now **refuses to
  start** if `idl/vanna_lending.json` doesn't hash to `PINNED_IDL_SHA256`.
  That program is layer HEAD `ecdb9b6` (D8).
- **Lend/borrow assets:** only SOL/wSOL, USDC, USDT can be lent (Earn) or
  borrowed. JitoSOL and JupSOL are collateral, swap and Kamino-farm only. Their
  on-chain reserves are live and borrow-enabled, so the MCP enforces this
  itself: `supply`, `borrow`, `open_debt` and the borrow leg of
  `deposit_and_borrow` return `asset_not_offered` for them (exits — `redeem`,
  `repay`, `close_debt` — stay open).
- **Oracle:** the app's price keeper (Jupiter USD prices written to the fork's
  oracle accounts; JupSOL priced from Jupiter) is the mechanism for this
  release, and the MCP calls it. Hermes/`pyth-pull` is **not used** — the
  Hermes settings were removed.
- **Signing:** the existing flow is build → simulate → connected wallet signs
  (it is also the fee payer, incl. lookup-table setup) → app broadcasts to the
  hosted RPC → confirm. There is no HTTP Sign Service and no auto-sign, so
  `vanna_sign` is `tx_status` only and the Sign Service settings were removed.
- **Swaps and farming:** already implemented and UI-connected (Jupiter wallet
  swaps, margin swaps via `margin_execute`, Kamino single-asset farm supply and
  withdraw from the margin balance, no leverage). No multi-asset Kamino.

## What's *not* covered — on purpose

- **Admin/authority instructions** (`admin_*`, `initialize_protocol`,
  `authority_accept_admin`) — never wrapped.
- **`public_liquidate` execution** — the contract instruction is permissionless,
  but the backend has no user-facing liquidation wrapper or UI. Only the read
  side ships (`vanna_margin_status.liquidation_snapshot`). `ENABLE_LIQUIDATION_BUILDER`
  is a reserved flag; no builder exists.
- **`public_refresh_reserve`** — not wrapped; reads project interest accrual to
  the chain clock in memory.
- **Server-side signing** (`sign_and_submit`, auto-sign, signer binding) —
  removed by decision (D3); a remote signer would be a new adapter, only if a
  later phase asks for one.
- **Hermes / production Pyth adapter** — not used this release (D4).
- **Temporary-ALT fallback for oversized Jupiter routes** — the MCP returns
  `transaction_too_large` instead; the caller retries with a smaller amount.

## Testing

Two groups of tests:

- **Offline (83, no network, seconds):** `test_foundation.py`,
  `test_pricing.py`, `test_health_accounts.py`, `test_hosting.py`,
  `test_price_keeper.py`, `test_read_cache.py`.
- **Live:** every other file hits the hosted Surfpool fork over the network,
  with no mocks. These haven't run since the `ecdb9b6` port because the fork
  was down. Several still assert the pre-port asset set and must be updated
  from the first live registry read (see `HANDOFF.md`).

In the live tests, read-dispatcher tests query real on-chain accounts; write-dispatcher
tests build and simulate real v0 transactions; the deepest ones (composites,
full flows) actually sign and submit with a disposable, freshly-airdropped
keypair to create real fixture state, then read it back. The MCP server code
itself never signs — only `tests/_live_helpers.py` does, and it's explicitly
test-only.

```bash
# Full suite (~7-8 minutes — network-bound, not CPU-bound)
./.venv/Scripts/python.exe -m pytest tests/ -q

# One file at a time (useful while iterating)
./.venv/Scripts/python.exe -m pytest tests/test_margin_trade_dispatcher.py -v

# Just the offline, no-network tests (fast — seconds)
./.venv/Scripts/python.exe -m pytest tests/test_foundation.py tests/test_pricing.py tests/test_health_accounts.py tests/test_hosting.py tests/test_price_keeper.py tests/test_read_cache.py -q
```

On Linux/macOS, drop the `./.venv/Scripts/python.exe` prefix for
`.venv/bin/python` (or just `pytest` once the venv is activated).

### Test file map

| File | Covers |
|---|---|
| `test_foundation.py` | Offline: math formulas vs. hand-computed examples, layout round-trips, PDA derivation, known-mint registry fallback — no RPC |
| `test_pricing.py` | Offline: the oracle facade (Scope / Pyth fallback / receipt rate, freshness/TWAP/confidence flags) |
| `test_health_accounts.py` | Offline: the `remaining_accounts` health tail |
| `test_hosting.py` | Offline: scopes table vs. dispatcher actions, OAuth middleware, write-scope gating, rate limit, circuit breaker, real HTTP sessions |
| `test_price_keeper.py` | Offline: the keeper refresh source (throttle, shared call, errors) |
| `test_read_cache.py` | Offline: read cache TTL, per-key lock, error results never cached |
| `test_protocol_info_dispatcher.py` | `vanna_protocol_info` |
| `test_oracle_dispatcher.py` | `vanna_oracle` (incl. Kamino-receipt pricing) |
| `test_oracle_refresh.py` | Fork price refresh, incl. the borrow-auto-refresh proof |
| `test_margin_max_actions.py` | `max_borrow` / `max_withdraw` accepted at the max, refused above |
| `test_earn_dispatchers.py` | `vanna_earn_market`, `vanna_earn_position`, `vanna_earn_write` |
| `test_account_dispatcher.py` | `vanna_account` (all 5 actions) |
| `test_margin_status_dispatcher.py` | `vanna_margin_status` |
| `test_margin_trade_dispatcher.py` | `vanna_margin_trade`'s single-step actions, input validation, fail-closed edge cases |
| `test_margin_composites.py` | `deposit_and_borrow` and `close_position` — full atomic and multi-step live flows on a dedicated disposable position |
| `test_wallet_dispatcher.py` | `vanna_wallet` |
| `test_history_dispatcher.py` | `vanna_history` |
| `test_farm_dispatchers.py` | `vanna_farm_overview`, `vanna_farm_kamino` |
| `test_swap_dispatcher.py` | `vanna_swap` (quote, can_swap, margin_swap, wallet_swap) |
| `test_sign_dispatcher.py` | `vanna_sign` (`tx_status`; server-side signing actions don't exist) |
| `test_fork_dispatcher.py` | `vanna_fork` (dev-only faucet) |

### Fixtures worth knowing about (`tests/conftest.py`)

- `ctx` — a fresh `VannaContext` per test function.
- `funded_fork_pubkey` / `funded_fork_keypair` — a disposable keypair,
  airdropped SOL on the fork, **session-scoped** (shared across the whole
  test run — created once, reused everywhere).
- `live_margin_position` — a real, **session-scoped, shared** margin account
  (1 SOL collateral, 1 USDC debt) built once and reused by read-only or
  non-destructive tests. **Never drain, close, or otherwise mutate this
  position's collateral/debt count in a new test** — it'll break every other
  test that runs after it in the same session. `test_margin_composites.py`
  deliberately builds its **own** disposable keypair + margin account instead,
  for exactly this reason.

### Known, expected flakiness (not a regression if you see it)

Jupiter routes get quoted against Jupiter's live mainnet state, but the
Surfpool fork's cloned DEX pools can drift from that state under concurrent
use. `margin_swap`/`wallet_swap` tests retry a few times on the recognized
symptom (`tests/_live_helpers.py::is_stale_fork_pool_error`,
`is_stale_price_error`) and `pytest.skip` — never fail — if every attempt
hits it. If the full suite reports `... passed, 1 skipped`, that's this,
not a bug.

### Adding a new test for a write action

Follow the existing pattern: build the envelope via the dispatcher function
(never the contract class directly), assert `simulation_success`, then only
sign+submit with `tests/_live_helpers.py::sign_and_submit` if the test needs
real resulting on-chain state for something downstream to read. Use
`build_with_retry` around anything health-checked (borrow, withdraw,
margin_swap, farm_kamino) to absorb the fork's own price-staleness window
rather than asserting success on the first attempt.
