# Vanna Solana MCP — Build Requirements

## Part A — Asks for Senior (need answers before/while buildi

**1. Canonical program ID + IDL for the target deployment**
- Program ID (base58) for the cluster the MCP should point at (devnet/testnet/mainnet)
- Authoritative IDL file (confirm it matches what's actually deployed)
- Which cluster is the launch target

**2. Final asset list**
- Per asset: symbol, mint address, decimals, Pyth feed ID, LTV bps, liquidation threshold bps, liquidation bonus bps
- Confirm if any assets get added before MCP ships (yes/no + which)
- If yes: should the MCP discover assets dynamically on-chain, or is a hardcoded list fine for v1

**3. Signing/broadcast service interface**
- Exact request schema it expects (tx format: base64? versioned tx? legacy?)
- Exact response schema it returns
- Endpoint + auth mechanism

**4. Pyth Hermes access for production**
- API key (env var) the MCP should use
- Rate limit / quota budget
- Confirm: separate key from frontend, or shared

**5. Liquidation — in scope or not**
- Yes/no: should this MCP expose the liquidate instruction
- If yes: who calls it (keeper bot identity/wallet, or any agent)

**6. Health factor threshold — final or provisional**
- Confirm 1.1 is the final liquidation threshold for all assets, or per-asset
- Source of truth to cite if it changes later

**7. Admin instructions — in scope or not**
- Confirm: no end-user/agent-facing wrapper needed for `admin_*` / `initialize_protocol` / `authority_accept_admin`

**8. Faucet — in scope or not**
- Yes/no: does the MCP need devnet faucet support (SOL airdrop, USDC faucet link)

---

## Part B — Contract wrappers to build

One program (`vanna_lending`, IDL: `idl/vanna_lending.json`), split internally into wrapper modules by instruction group.

### 1. `ProtocolConfigContract`
- **PDA:** `["protocol"]`
- **Reads:** `ProtocolConfig` → `admin`, `pending_admin`, `treasury`, `operating_mode`, `max_assets_per_margin`, `next_asset_index`
- **Instructions (admin-only, build only if Ask #7 says yes):** `initialize_protocol`, `admin_propose_authority`, `authority_accept_admin`, `admin_set_operating_mode`, `admin_collect_protocol_fees`

### 2. `AssetConfigContract`
- **PDA:** `["asset", mint]`
- **Reads:** `AssetConfig` → `mint`, `reserve`, `price_feed_id`, `max_collateral_per_margin`, `ltv_bps`, `liquidation_threshold_bps`, `liquidation_bonus_bps`, `max_confidence_bps`, `max_price_age_secs`, `asset_index`, `decimals`, `collateral_enabled`, `borrow_enabled`
- **Instructions (admin-only, build only if Ask #7 says yes):** `admin_register_asset`, `admin_update_asset_config`

### 3. `ReserveContract` (Earn / lending pool)
- **PDAs:** `reservePda(mint) = ["reserve", mint]`, `shareMintPda(mint) = ["share_mint", mint]`
- **Reads:** `Reserve` → `underlying_mint`, `liquidity_vault`, `share_mint`, `supply_cap`, `borrow_cap`, `accounted_liquidity_assets`, `total_borrow_assets`, `total_borrow_shares`, `borrow_index_wad`, `accrued_protocol_fees`, `base_rate_bps`, `slope1_bps`, `slope2_bps`, `optimal_utilization_bps`, `reserve_factor_bps`, `status`
- **Instructions:** `lender_supply` (deposit, mint shares), `lender_redeem` (burn shares, withdraw), `public_refresh_reserve` (permissionless accrual tick)
- **Admin-only (build only if Ask #7 says yes):** `admin_initialize_reserve`, `admin_update_reserve_config`
- **Derived math to implement:** utilization rate, borrow/supply APY, exchange rate (v-token → underlying) — same formulas as `lib/pool-stats.ts`

### 4. `MarginAccountContract`
- **PDA:** `marginPda(authority) = ["margin", authority]` — one per wallet, not per position
- **Reads:** `MarginAccount` → `authority`, `status`, `collateral_count`, `debt_count`, `collateral_asset_indexes[8]`, `debt_asset_indexes[8]`, `event_sequence`
  - Must resolve each index back to a mint/symbol via the matching `AssetConfig.asset_index`
- **Instructions:** `user_create_margin`, `user_close_margin`

### 5. `CollateralContract`
- **Vaults (not PDAs):** ATA of `mint` owned (off-curve) by the margin account — `getAssociatedTokenAddressSync(mint, marginPda, true)`
- **Instructions:** `user_deposit_collateral`, `user_withdraw_collateral` (needs a fresh Pyth price account + remaining-accounts for every other open collateral/debt position on that margin account), `user_close_collateral_position`

### 6. `DebtPositionContract` (Borrow)
- **PDA:** `debtPositionPda(margin, reserve) = ["debt", margin, reserve]`
- **Reads:** `DebtPosition` → `margin_account`, `reserve`, `borrow_shares`
  - Raw debt amount = `borrow_shares * reserve.total_borrow_assets / reserve.total_borrow_shares`
- **Instructions:** `user_open_debt_position` (create before first borrow of that asset), `user_borrow` (needs fresh Pyth price + remaining-accounts), `user_deposit_and_borrow` (composite: deposit one asset, borrow a different asset, one instruction), `user_close_debt_position` (only valid at zero debt)

### 7. `RepayContract`
- **Instructions:** `user_repay_from_margin` (repay using the margin account's own balance), `public_repay_from_wallet` (anyone can repay on behalf of a margin account directly from their wallet)

### 8. `LiquidationContract` (build only if Ask #5 says yes)
- **Instructions:** `public_liquidate` (permissionless; repay a portion of an unhealthy margin's debt, seize discounted collateral)
- **Needs a pre-check** (off-chain, no dedicated on-chain view): health factor <= threshold from Ask #6, using the same math as `MarginAccountContract` reads — must fail closed (deny) on any read error before ever building a liquidate instruction

### 9. `OracleContract` (Pyth)
- **Programs:** Pyth Receiver `rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ` (owns the `PriceUpdateV2` account), Pyth push-oracle `pythWSnswVUd12oZpeFP8e9CVaEqJg25g1Vtc2biRsT` (derives the price-feed PDA)
- **Reads:** decode `PriceUpdateV2` account → price, exponent, publish time; validate feed ID + account owner
- **Write:** fetch signed update from Hermes, post on-chain via `@pythnetwork/pyth-solana-receiver` before any borrow/withdraw/deposit_and_borrow/liquidate call — must run automatically inside those wrapper calls, not as a separate manual step, or the on-chain simulation fails on a stale price

---

## Notes carried over from analysis (context for the spec above)
- No registry contract exists — one hardcoded program ID + a static asset table is enough for v1; no TTL-cached address-resolution layer needed.
- No swap/farm/external lending integration exists anywhere in the repo — nothing to wrap beyond the 9 modules above.
- Every write-path wrapper must return an **unsigned transaction + simulation result** — it never signs or submits (see Ask #3 for where it hands off).
