# HANDOFF - continue the Vanna Solana MCP port (written 2026-09-29)

A Python MCP server for the Vanna Finance lending protocol on Solana. **The deployed program changed under us;
the code has been ported to it but nothing is verified live yet.** Read this file first, then `DECISIONS.md` (every
team decision, what it changed in code, open items O2–O4, and the gate for Copilot building), then `CURRENT_STAGE.md`.

## Rules (from the owner)
- **Only modify files inside `D:\Projects\Vanna_MCP_Solana`.** The sibling repos are READ-ONLY references:
  `D:\Projects\vanna-credit-layer` (Rust program = the authority), `D:\Projects\vanna-credit-backend` (Next.js app),
  `D:\Projects\Vanna_MCP\vanna-mcp` (Stellar MCP, the model this was built after).
- The MCP never signs or broadcasts; every write returns an unsigned v0 transaction envelope.
- Don't commit unless asked. Nothing is committed yet (large uncommitted change set; `git status` shows it).
- Don't assume: verify against the Rust source and live accounts (that is how every bug so far was found).
- Update the related docs in the same change as the code (the owner asked for this explicitly); decisions go in `DECISIONS.md`.

## Start here (first 3 commands)
```bash
cd /d/Projects/Vanna_MCP_Solana
./.venv/Scripts/python.exe -m pytest tests/test_read_cache.py tests/test_price_keeper.py tests/test_hosting.py tests/test_pricing.py tests/test_health_accounts.py tests/test_foundation.py -q   # 83 offline tests, must pass
curl -s -X POST https://rpc-devnet.solana.vanna.finance -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"getSlot"}'   # is the fork RPC up? (it was 502 for hours)
./.venv/Scripts/python.exe handoff/program_probe.py    # is the program + ProtocolConfig + each AssetConfig/Reserve on the fork?
./.venv/Scripts/python.exe handoff/live_registry.py    # once they exist: decodes every live AssetConfig with the NEW layout
```

## What happened
The layer repo got commits on 2026-09-29 (`ce138ca` whole-account liquidation + new asset set, `64c4c84` oracle rework;
HEAD `ecdb9b6`) and the hosted fork now runs that program (team confirmed it is permanent). The old IDL (including the
backend's copy) no longer describes it. The owner re-confirmed on 2026-09-29 that the fork runs layer HEAD `ecdb9b6`
(`DECISIONS.md` D8), so the port stays; the backend's committed IDL lags and is not a layout reference. Verified from Rust source + a live account:
- `AssetConfig` is now **340 bytes** (was 258) and holds `oracle: OracleConfig` (Scope chain, Pyth fallback, optional
  second Pyth feed, klend receipt reserve/program, max age, TWAP divergence bps, confidence bps).
- **Pricing** (`oracle/mod.rs`): Scope chain first; Pyth fallback (x optional 2nd feed, e.g. JupSOL = JUPSOL/SOL x SOL/USD)
  when Scope is stale/unused; x klend cToken rate for receipts. Flags FRESH / TWAP_OK / CONFIDENCE_OK. Borrow, and
  withdraw / `margin_execute` while in debt, need ALL; liquidation needs FRESH only; withdraw with **no debt reads no prices**.
- `user_borrow`, `user_withdraw_collateral`, `margin_execute` **no longer take price accounts**. `remaining_accounts` =
  (margin_execute: CPI accounts first, then) position groups `[asset_config, vault]` / `[asset_config, reserve, debt_position]`
  in slot order with NO prices, then the oracle accounts of every asset involved, each once, any order.
- `public_liquidate()` has no args: whole-account, no close factor/bonus. New event `CollateralSeized`; `Liquidated` reworked.
- Layer `scripts/bootstrap-fork.sh` says: lending pools SOL/USDC/USDT; collateral-only JitoSOL, JupSOL, JupUSD, NVDAx, TSLAx
  (xStocks are **Token-2022**); Kamino receipts only for USDC and SOL. **NOT yet verified on the live fork.**

## Status
**Done and verified offline (45 tests pass):**
- [x] `vanna_core/pricing.py` - 1:1 port of the oracle facade. `vanna_core/oracle_reader.py` - batched fetch -> `OracleView`.
- [x] `layouts.py` new `OracleConfig`/`AssetConfig`/events; `assets.py` discovers every AssetConfig via `getProgramAccounts`
      (`DiscoveredAsset.oracle`, `.is_receipt`, `.has_reserve`); `client.py` retrying RPC proxy + `get_program_accounts`.
- [x] `remaining_accounts.py` (new health tail), `margin_execute.py`, `margin_trade.py` (borrow / withdraw / deposit_and_borrow /
      close_position), `farm_kamino.py` (now uses the shared margin_execute builder), `swap.py`, `farm.py`, `wallet.py`,
      `protocol.py`, `history.py`, `oracle.py`, `margin.py` (rewritten: health via pricing, previews, `max_borrow`/`max_withdraw`,
      whole-account `liquidation_snapshot`).
- [x] `contracts/oracle_refresh.py` dev-fork adapter (ported from layer `scripts/src/oracle.ts`): fabricates the Pyth
      `PriceUpdateV2` accounts each asset pins + copies Kamino's Scope account from mainnet (`MAINNET_RPC_URL`), restamping
      used entries to the fork clock, via `surfnet_setAccount`.
- [x] `idl/vanna_lending.json` regenerated from layer source (`cargo test --features idl-build` on a scratch copy; see
      `handoff/assemble_idl.py`); pin `pinned_idl_sha256 = 47cd9ced07539538e42e859b99ba535720a030371399a11ff732b6bc817ffe73`
      in `vanna_core/config.py` (server refuses to start on mismatch). It lacks Anchor-CLI PDA/address annotations (unused).
- [x] Product policy: only SOL/USDC/USDT lend/borrow (`asset_not_offered`); errors `price_unavailable`, `transaction_too_large`.

**BLOCKED:** the hosted fork RPC returned HTTP 502 for hours, so **no live test has run since the port**.
Still 502 on 2026-09-29 13:55 IST; the deployed app `test.solana.vanna.finance/api/pools` fails with the same 502 reading
the Clock sysvar, so the fork node itself is down. No local `surfpool` / `solana` CLI on this machine. Layer HEAD still `ecdb9b6`.
14:00–14:20 IST: the fork came back half-alive. `getHealth` returns `ok` in <1 s, but `getSlot`, `getAccountInfo`,
`getProgramAccounts` and `getMultipleAccounts` all hang past 60 s, and `test.solana.vanna.finance/api/pools` times out at 90 s.
`handoff/rpc_probe.py` times these calls; re-run it until `getSlot` answers before starting step 1.
14:45 IST re-check: `rpc-devnet.solana.vanna.finance` (34.30.21.194) reports `surfnet-version 1.5.0` / `solana-core 4.1.2` and
answers `getHealth` + `getVersion` instantly, but every state read hangs, so Surfpool's state engine is stuck.
Hosts: `rpc-devnet.solana.vanna.finance` = the fork RPC (layer `Anchor.toml`, backend, MCP all use it; correct);
`devnet.solana.vanna.finance` (34.117.40.168) = the **old stack's** web app (its `/api/pools` returns base-rate/slope/kink
fields that `vanna-credit-backend` never emits); `test.solana.vanna.finance` = the `vanna-credit-backend` Cloud Run app
(`cloudbuild.yaml`). `PRICE_KEEPER_URL` (keeper + faucet) was changed to `test.solana.vanna.finance` on 2026-09-29.

**Fork status at the end of 2026-09-29 (re-probed, `handoff/program_probe.py`):** the RPC answers again (`getSlot` 0.3 s),
but the fork was restarted with **empty state**: the program account `H37YVBG3…`, `ProtocolConfig` and every
`AssetConfig`/`Reserve` are missing (Kamino exists only because Surfpool clones mainnet on demand), and
`test.solana.vanna.finance/api/pools` reports every pool `initialised: false`. `getProgramAccounts` returns "Internal error".
Nothing can be verified live until the team redeploys the program and runs the bootstrap on this fork.

**Hosting layer ported from the Stellar MCP (2026-09-29, offline, 27 tests in `tests/test_hosting.py`):**
- [x] `mcp_server/scopes.py` - one read/write table per dispatcher action (`WRITE_ACTIONS`, `WRITE_ONLY_TOOLS`,
      `EXPENSIVE_READ_TOOLS`); a test pins it to each dispatcher's `action` Literal.
- [x] `mcp_server/identity.py` - verified claims + end-user assertion stamped on the ASGI scope; bound per tool call from
      `ServerRequestContext.request` (mcp 2.x; Stellar used 1.x `request_ctx`). Write scope = `vanna:write`/`write`/`*`/`vanna:*`
      or a `FIRST_PARTY_CLIENT_IDS` client; unauthenticated = write only when `AUTH_ENABLED=false`.
- [x] `mcp_server/auth.py` - JWKS `TokenVerifier`, `AuthMiddleware` (Origin check, bearer, `X-Vanna-User-Assertion`, machine
      bearers never forwarded), RFC 9728 metadata. Stellar's WorkOS-email -> Privy-DID resolver is NOT ported: the wallet
      signs, there is no Sign Service or Privy (D3).
- [x] `mcp_server/rate_limit.py` - pure-ASGI token bucket on `POST /mcp` (write 10 / heavy read 5 / read 1) + circuit breaker.
      Stellar's first-party per-user buckets are not ported (keyed on Stellar wallets).
- [x] `mcp_server/agent_firewall.py` - Solana domain instructions (the old text named a non-existent `unsigned_tx_b64` field).
- [x] `mcp_server/main.py` - `ScopedMCPServer` hides write-only tools and refuses write actions without write scope, applies the
      breaker (`internal_error` results count); discovery routes + `/healthz`; `build_streamable_http_app()`. **Fixed:** the old
      `streamable_http_app()` call used the SDK default host `127.0.0.1`, which auto-enables localhost-only Host checks and would
      have rejected every hosted request.
- [x] `Dockerfile`, `.dockerignore`, `cloudbuild.mcp.yaml` (project/service names are placeholders), settings in `config.py` +
      `.env.example`, `uvicorn`/`starlette`/`PyJWT[crypto]` in `requirements.txt`. Docker build not run on this machine.
- Lifespan concern resolved: mcp 2.2 `StreamableHTTPSessionManager.run()` enters the app lifespan once per process, not per session.
- Not ported: `onchain_strings.py` (Solana tools surface no free-text on-chain strings today).

**Team decisions of 2026-09-29 applied (register: `DECISIONS.md`; offline tests now 83):**
- [x] D3 signing: `vanna_sign` = `tx_status` only (phase-4 actions removed → `invalid_input`); `vanna_wallet.connect_status`
      removed; `SIGN_SERVICE_*`/`S2S_AUTH_MODE` settings removed; `scopes.py` no longer lists `vanna_sign` writes.
- [x] D4 pricing: `PRICE_REFRESH_SOURCE=keeper` (default) calls the app's `POST /api/prices/refresh` (60 s throttle, shared
      in-flight call, `tests/test_price_keeper.py`); the old cheatcode writer is opt-in (`cheatcode`); `PYTH_HERMES_URL`/
      `PYTH_API_KEY` removed; `pyth-pull` = no refresh.
- [x] `mcp_server/read_cache.py` (30 s TTL, per-key lock, error results never cached) on protocol info, oracle prices, pool
      stats, farm markets (`tests/test_read_cache.py`; `conftest.py` disables it for the other tests).
- [x] D2/D5/D6 were already true in code (asset policy, read-only liquidation, margin-balance farm) — docs aligned.
- [x] Docs: `DECISIONS.md` (new), `TOOLS_CATALOGUE.md` (status banner, §0.1/§2.1/§2.3/§3.10/§3.13/§3.14/§4/§6.2/§7.1),
      `COPILOT_ARCHITECTURE.md` Part B, `BUILD_PLAN.md`, `CURRENT_STAGE.md`, `README.md`, `.env.example`.

Backend pulled to `f643dbf` (`4990897` Kamino update, `f643dbf` GCP setup: Dockerfile + `cloudbuild.yaml` → Cloud Run
`test.solana.vanna.finance`, same fork RPC and program `H37YVBG3…`). Relevant to the MCP: `TokenInfo.lendable` (SOL/USDC/USDT
only for Earn supply and borrow) matches our `LEND_BORROW_SYMBOLS`; the Farm page is now margin-balance only, no leverage, which
the MCP's `farm_kamino.deposit` already is. **Its `idl/` was not updated and its token list is still the old five assets**, so the
backend still lags the new program; keep using the layer source as the authority.

**Done offline on 2026-09-29 (then 74 offline tests: `test_hosting`, `test_pricing`, `test_health_accounts`, `test_foundation`):**
- [x] `AssetRegistry.load()` falls back to the known-mint list (`known_mints()`: catalogue + `EXTRA_ASSET_SYMBOLS` + receipts)
      when `getProgramAccounts` fails; each PDA is validated (owner, 340-byte length, discriminator). `registry.discovery_mode`
      reports `program_accounts` or `known_mints`. Tests in `test_foundation.py`.
- [x] `test_protocol_info_dispatcher.py`: USDC `price_source` now `"Oracle"`. Its asset-set assertion, `test_wallet_dispatcher`
      (5 assets), `test_farm_dispatchers` (5 receipts) and `test_registry_has_exactly_five_real_assets...` are deliberately left
      until step 1 shows the live set (bootstrap intends SOL/USDC/USDT, JitoSOL, JupSOL, JupUSD, NVDAx, TSLAx, kUSDC, kSOL).

**Todo, in order:**
1. **Live registry check.** Run `handoff/live_registry.py`. Expect ~10 assets; SOL idx 3, USDC 13, USDT 16, JitoSOL 210, JupSOL 224
   (asset_index = Scope entry). Confirm which Kamino receipts exist; then prune the stale static `KAMINO_RECEIPTS` in
   `assets.py` (3 of 5 probably unregistered) and fix `test_registry_has_exactly_five_real_assets...` in `tests/test_foundation.py`.
   If `getProgramAccounts` is unsupported by the fork, fall back to the known-mint list.
2. **Full suite** `./.venv/Scripts/python.exe -m pytest tests/ -q` (~7 min, hits the fork). Expected fixes: `test_protocol_info_dispatcher`
   (asserts the old 10-asset set and `price_source == "Pyth"`; now `"Oracle"`), `test_wallet_dispatcher` (asserts 5 assets), `test_oracle_refresh`,
   `test_oracle_dispatcher` (kUSDC receipt), `test_farm_dispatchers`, swap and composite tests. **The live borrow/withdraw/margin_execute
   simulations are the real proof that the new account lists are right** - if a simulation fails, read the program log + `risk_engine.rs`.
   Step 1 also confirms **D8** live: expect 340-byte `AssetConfig`s (`ecdb9b6`). If discovery finds nothing because the
   accounts are 258 bytes, the fork was bootstrapped with the older program; stop and raise it with the owner.
3. Verify `maybe_refresh_prices` makes prices fresh so `borrow` simulates. The default is now the app's keeper
   (`PRICE_KEEPER_URL`), which writes Pyth accounts only; on `ecdb9b6` (Scope first) check it is enough, else set
   `PRICE_REFRESH_SOURCE=cheatcode` and record it in `DECISIONS.md` D4. Check
   `tests/test_oracle_refresh.py::test_borrow_auto_refreshes_without_manual_step`.
4. Run the new `tests/test_margin_max_actions.py` (max_borrow / max_withdraw are accepted at the max, refused above).
5. **Docs after step 1:** record the live asset set in `DECISIONS.md` D2 and `CURRENT_STAGE.md`, the live confirmation in
   `DECISIONS.md` D8, and the live test count in `CURRENT_STAGE.md`/`BUILD_PLAN.md`. Then consider
   `handoff/apply_catalogue_notice.py` (it adds a "superseded by the program update" table to `TOOLS_CATALOGUE.md`; check
   its asset row against step 1 first — the catalogue already has a status banner pointing at `DECISIONS.md` D8).
6. One wallet-signed round trip through the app (sign → broadcast → `vanna_sign.tx_status`) — part of the Copilot gate.

## Key files
`vanna_core/` protocol logic (`pricing.py`, `oracle_reader.py`, `assets.py`, `layouts.py`, `client.py`, `remaining_accounts.py`,
`margin_execute.py`, `math_utils.py`, `contracts/*`), `mcp_server/` (dispatchers in `dispatchers/`, `error_handling.py`, `dependencies.py`),
`tests/` (live fork tests + `_live_helpers.py`; `conftest.py` has the shared `live_margin_position` fixture - never drain it),
`idl/`, docs: `DECISIONS.md`, `README.md`, `CURRENT_STAGE.md`, `BUILD_PLAN.md`, `TOOLS_CATALOGUE.md`, `COPILOT_ARCHITECTURE.md`.

## Pitfalls
- Bash heredocs containing quotes/apostrophes fail here; write scripts to a file and run them. The shell tool collapses `\\` to `\`,
  so build a backslash with `chr(92)` (this once wrote a NUL byte into `client.py`).
- The fork is flaky (502 / "Internal error"); the client retries transient errors. Jupiter-route tests may skip on fork DEX-pool drift (expected).
- Session-scoped test fixtures share one margin account; use a fresh keypair for destructive tests (see `test_margin_composites.py`).
- Known gaps: the backend's own IDL is stale (its app likely fails borrow/withdraw on the new program - unconfirmed); parity check against the
  backend's e2e scripts was skipped; temporary-ALT fallback for oversized txs not built; liquidation execution (D5) and any Sign Service / Hermes (D3, D4) are out of scope by decision.
