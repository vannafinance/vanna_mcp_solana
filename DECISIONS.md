# Decisions register — Vanna Solana MCP

This file records every project-level decision the MCP is built on, where each one came from, and
what it changed in this repo. The other docs point here instead of restating the decisions. If a
decision changes, update this file, the code, and the docs in the same change.

Sources:

- **PDF** — `Vanna-Solana-Copilot-Answers.pdf` (28 Sep 2026, in this repo).
- **Team 2026-09-29** — the owner's follow-up answers on cluster, assets, signing, Hermes,
  liquidation and swap/farm (quoted in the entries below).

Status labels: **Settled** (decided, applied in code and docs) and **Open** (needs an answer or a
live check; the entry says what depends on it).

---

## Settled

### D1 — Target cluster and program (Team 2026-09-29) — Settled

- Target: the hosted Surfpool mainnet fork `https://rpc-devnet.solana.vanna.finance`, program
  `H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC`.
- Per the team: the live executable matches the pinned local build, the backend IDL equals the
  build IDL, and `ProtocolConfig` is initialized.
- **In this repo:** `SOLANA_RPC_URL`, `VANNA_PROGRAM_ID` and `SOLANA_CLUSTER_LABEL=surfpool-fork`
  are the defaults (`vanna_core/config.py`, `.env.example`). The server refuses to start when
  `idl/vanna_lending.json` doesn't hash to `PINNED_IDL_SHA256` (`mcp_server/dependencies.py`).
- The program version is layer HEAD `ecdb9b6` (**D8**).
- **Live state (2026-09-29, re-bootstrapped):** the fork was restarted empty earlier that day and has since
  been redeployed and bootstrapped. `handoff/program_probe.py` shows the program, `ProtocolConfig` (237 B),
  and 13 `AssetConfig`s (340 B each); only SOL/USDC/USDT have a `Reserve`. `getProgramAccounts` returns
  "Internal error" on this fork, so the registry falls back to the known-mint list (see D2).
- The app host is `https://test.solana.vanna.finance` (`vanna-credit-backend` on Cloud Run).
  `devnet.solana.vanna.finance` serves the old stack. `PRICE_KEEPER_URL` now defaults to the
  former; it previously pointed at the old stack.

### D2 — Assets (Team 2026-09-29) — Settled

| Asset | Earn lend | Margin borrow | Collateral | Swap | Kamino farm |
|---|---|---|---|---|---|
| SOL (wSOL), USDC, USDT | yes | yes | yes | yes | yes |
| JitoSOL, JupSOL | no | no | yes | yes | yes |
| Kamino receipts (kTokens) | — | — | yes | — | (the farm's output) |

- JupSOL is priced on the fork by the app's keeper from Jupiter USD prices (see D4). That is not
  production Pyth.
- **In this repo:**
  - `LEND_BORROW_SYMBOLS = {SOL, USDC, USDT}` and `assert_lend_borrow_offered` live in
    `vanna_core/assets.py`.
  - Supply, `open_debt`, borrow, `deposit_and_borrow`, `can_borrow` and `max_borrow` refuse other
    assets with error code `asset_not_offered`, even though the on-chain JitoSOL/JupSOL reserves
    are `borrow_enabled`.
  - Reads expose `lend_borrow_offered` per asset (`vanna_protocol_info.assets`, `vanna_wallet`).
- Asset parameters always come from the on-chain `AssetConfig`. The static table in `assets.py` is
  display data only.
- **Live asset set (2026-09-29, `handoff/live_registry.py`):** 13 registered assets, `asset_index` sequential
  in registration order (USDC 0, USDT 1, SOL 2, JitoSOL 3, JupSOL 4, JupUSD 5, NVDAx 6, TSLAx 7, kUSDC 8,
  kSOL 9, kUSDT 10, kJitoSOL 11, kJupSOL 12).
  - SOL/USDC/USDT: Reserve + borrow enabled. Every other asset: collateral only, `borrow_enabled = false`,
    no Reserve. So Earn reads (`pool_stats`, `deposited_balance`) also return `asset_not_offered` for them.
  - All five Kamino receipts are registered (not just USDC/SOL as the layer bootstrap suggests).
  - **JupUSD, NVDAx and TSLAx** exist on-chain as collateral-only assets, but the app's own token list
    does not have them yet: whether they are product scope is open (**O5**). The MCP handles them
    generically; NVDAx/TSLAx are Token-2022 and verified live (`tests/test_token2022_collateral.py`).

### D3 — Signing and execution (Team 2026-09-29) — Settled

- Flow: build → simulate → the connected wallet signs (`signTransaction(Transaction |
  VersionedTransaction)`, v0) → hosted-RPC broadcast → confirmation.
- The wallet is the fee payer, including lookup-table setup.
- There is **no HTTP Sign Service**, no Bearer-auth signing endpoint, no Privy and no auto-sign.
  The Copilot uses the app's existing wallet flow. A remote Sign Service adapter would be new work,
  only if a later phase explicitly asks for one.
- **In this repo:**
  - Every write returns an unsigned v0 envelope with `signing_status: needs_wallet_sign` (or
    `not_signable` while a setup dependency is outstanding). The MCP never signs or broadcasts.
  - `vanna_sign` has only `tx_status`. The phase-4 actions (`sign_and_submit`, auto-sign session,
    signer binding) were removed, and any other action returns `invalid_input`.
  - `vanna_wallet.connect_status` (an auto-sign capability check) was removed.
  - The `SIGN_SERVICE_URL`, `SIGN_SERVICE_SECRET` and `S2S_AUTH_MODE` settings were removed.
  - `mcp_server/auth.py` doesn't port Stellar's Privy resolver. The forwarded end-user assertion
    (`x-vanna-user-assertion`) is attribution only and never authorizes a transaction: the wallet
    signature does.

### D4 — Prices on the fork; no Hermes (Team 2026-09-29) — Settled

- Hermes is not used. The app's price keeper (`JUPITER_API_KEY`, with a previous/reference-price
  fallback) writes Jupiter USD prices into the Surfpool oracle accounts. Public-mainnet oracle
  requirements are a separate, later concern.
- **In this repo (`vanna_core/contracts/oracle_refresh.py`):**
  - `ORACLE_MODE=dev-fork` (default): refresh before every health-checked write.
  - `PRICE_REFRESH_SOURCE=keeper` (default): `POST {PRICE_KEEPER_URL}/api/prices/refresh` (optional
    `PRICE_KEEPER_SECRET` as Bearer). It runs at most once per 60 s per process, and concurrent
    callers share one call. The endpoint also heals cloned Kamino reserves.
  - `PRICE_REFRESH_SOURCE=cheatcode`: the MCP's own `surfnet_setAccount` writer. It is only for a
    program version the keeper doesn't cover, and must never run alongside the keeper.
  - `ORACLE_MODE=pyth-pull`: no refresh at all (for a cluster with external keepers). No Hermes
    adapter is built or planned.
  - **Pyth-only supplement (found live 2026-09-29).** The app keeper writes only the SOL, USDC, USDT and
    JupSOL-rate feeds. **JupUSD** is priced by Pyth alone (no Scope chain), its feed goes stale after
    180 s, and the keeper doesn't touch it, so any borrow or in-debt withdrawal on an account holding
    JupUSD would fail closed. After the keeper call the MCP therefore refreshes, by cheatcode, exactly the
    Pyth accounts of Pyth-only assets that the keeper did not report writing (60 s throttle of its own).
    It never writes an account the keeper wrote, so there is still one writer per account. Every other
    asset has a Scope chain, and Scope on the fork is fresh by itself (Surfpool clones Kamino's live
    account; ages of ~90 s observed), so no Scope restamp is needed with the keeper source.
  - A failed refresh is logged and the build continues. A still-stale price then fails closed in
    simulation (`stale_price`); fabricated prices are never returned.
  - The `PYTH_HERMES_URL` and `PYTH_API_KEY` settings were removed.

### D5 — Liquidation (PDF; Team 2026-09-29) — Settled

- `public_liquidate` exists on-chain (permissionless), but the app has no user-facing wrapper or UI.
- **In this repo:** read-only `vanna_margin_status.liquidation_snapshot`. There is no liquidation
  write in any dispatcher. `ENABLE_LIQUIDATION_BUILDER=false`; a keeper builder, if ever wanted,
  would be separate and off by default.

### D6 — Swaps and Kamino farming (Team 2026-09-29) — Settled

- Implemented and connected to the UI: Jupiter wallet swaps, margin swaps via `margin_execute`, and
  Kamino single-asset supply/withdraw. **Never describe these as "pending".**
- The Farm UI supplies from the **margin balance**, with no wallet funding and no leverage. Kamino
  multi-asset (Liquidity) is not supported: that program rejects the CPI (`CPINotAllowed`).
- **In this repo:**
  - `vanna_swap.wallet_swap` and `margin_swap`, plus `quote` and `can_swap`.
  - `vanna_farm_kamino.deposit` supplies from an existing margin-collateral balance. Funding it is
    a separate `vanna_margin_trade.deposit`.
  - `vanna_farm_kamino.redeem` returns the underlying to the margin vault.

### D7 — Earlier PDF decisions still in force — Settled

- **Unsigned-only builder:** the MCP ports only the backend's instruction construction, never
  `sendInstructions`, `.rpc()`, `signTransaction` or `sendRawTransaction`.
- **Envelope schema v1:** decimal-string integers, `dependsOn`, and `not_run_dependency` for setup
  that must confirm first.
- **No admin wrappers:** protocol and asset configuration are read-only.
- **Faucet:** fork-only dev tool, registered only with `ENABLE_FORK_TOOLS=true` on a fork, and
  never in the Copilot's allowlist.
- **No arbitrary program invocation:** only the Kamino and Jupiter adapters, and only while their
  `Integration` is enabled.

### D8 — Program version: layer HEAD `ecdb9b6` (owner, 2026-09-29; was O1) — Settled

- **The question it answered.** Two sources disagreed.
  - The team said "the backend IDL equals the build IDL". But
    `vanna-credit-backend/idl/vanna_lending.json` at `f643dbf` has the older oracle model
    (`PriceSource`, `PriceUpdateV2`, no `OracleConfig`).
  - This repo had already been ported to layer HEAD `ecdb9b6`, because the team had earlier said
    the fork moved to it permanently.
- **Decision:** the fork will run layer HEAD `ecdb9b6`, so the port stays. The backend's committed
  IDL and token list lag behind that program and are not a reference for layouts.
- **In this repo:**
  - `idl/vanna_lending.json` is regenerated from `ecdb9b6` source (`handoff/assemble_idl.py`) and
    pinned as `PINNED_IDL_SHA256 = 47cd9ced…`.
  - Layouts: 340-byte `AssetConfig` with `OracleConfig`, plus the `CollateralSeized` and
    `AssetOracleUpdated` events.
  - `pricing.py` ports `oracle/*.rs`: Scope first, then Pyth fallback, then the Kamino receipt
    rate.
  - The health tail carries no price accounts; liquidation is whole-account.
  - The pre-port code, which targeted the older program, was never committed. It isn't needed now.
- **Confirmed live (2026-09-29):** `handoff/live_registry.py` decodes 340-byte `AssetConfig`s with the
  new layout, and the full live suite passes on the ported code (204 tests). The knock-on for D4 is
  resolved: Scope is fresh on the fork, so the keeper's Pyth writes are enough for every asset except
  the Pyth-only JupUSD, which the supplement covers (D4).

---

## Open

### O2 — Login provider for the MCP's OAuth — Open, blocks the hosted deploy

- The hosting layer is a standard OAuth 2.1 resource server: `OAUTH_ISSUER`, `OAUTH_JWKS_URI`,
  `OAUTH_AUDIENCE` and `FIRST_PARTY_CLIENT_IDS`.
- Which provider issues the Copilot's machine-to-machine token is not decided. The app has no
  Privy.
- Local and stdio runs use `AUTH_ENABLED=false` and don't need it.

### O3 — Hosting project for the MCP — Open, blocks the hosted deploy

- `Dockerfile` and `cloudbuild.mcp.yaml` are ready, with placeholders `_SERVICE=vanna-mcp-solana`,
  `_IMAGE_NAME=mcp-solana` and `_REPO=vanna-credit`.
- Still needed: the GCP project, region, service name and runtime service account (the app's own
  service is `vanna-credit-solana-test`).

### O5 — Are JupUSD, NVDAx and TSLAx product scope? — Open, not blocking

- They are registered on the fork as collateral-only assets (layer bootstrap), but the app's token list
  (`vanna-credit-backend`) and the team's asset answer (D2) name only SOL/USDC/USDT/JitoSOL/JupSOL.
- The MCP already supports them (deposit, valuation, withdraw; NVDAx/TSLAx are Token-2022). Swaps and
  the Kamino farm for them are untested and not offered by the app.
- Needed from the team: expose them to the Copilot, or filter them out (a one-line allowlist in
  `vanna_core/assets.py`).

### O4 — Per-user rate limiting behind the Copilot — Open, not blocking

- The rate-limit bucket is keyed by the token's `sub`, then its client id, then the client IP.
- The Copilot calls with one first-party machine token, so all Copilot users share one bucket.
- Per-user buckets would need the Copilot to forward a user identifier the MCP trusts for keying.
  Its format is not decided. Until then, the Copilot applies its own per-user limits.

---

## Gate for Copilot building

The Copilot (in `vanna-credit-backend`, `COPILOT_ARCHITECTURE.md` Part B) can start once:

0. **The fork has the program.** ✅ Done 2026-09-29 (`handoff/program_probe.py`).
1. **The live checks pass on `ecdb9b6` (D8).** ✅ Done 2026-09-29: the live registry decodes 340-byte
   `AssetConfig`s and the full live suite passes (204 tests, no skips).
2. **The live smoke checks pass:**
   - ✅ the keeper price refresh from the MCP (`handoff/price_check.py`: every asset fresh after it);
   - ✅ one build and simulation per write dispatcher, including `max_borrow`/`max_withdraw` (live suite);
   - ⬜ one wallet-signed round trip through the app (sign → broadcast → `vanna_sign.tx_status`). This is
     app-side (the wallet flow lives in `vanna-credit-backend`), so it is the first Copilot task.

**The MCP is ready for the Copilot.** The remaining gate item is app-side.

Work that can start in parallel, before the gate:

- The Copilot's direct lane, router, firewall vocabulary and UI against the frozen tool surface
  (`TOOLS_CATALOGUE.md` §3).
- The app-side execution adapter that reuses the existing wallet flow (Part B.9).

Needed only for the hosted deployment, not for building: O2 (the login provider) and O3 (the
hosting project). Until then, the Copilot can talk to a locally run MCP with `AUTH_ENABLED=false`.
