import pathlib

p = pathlib.Path("D:/Projects/Vanna_MCP_Solana/TOOLS_CATALOGUE.md")
s = p.read_text(encoding="utf-8")
marker = "`Backend-Solana` / `Backend-Solana-v2` are not references; they belong to the old stack.\n"
assert marker in s
notice = marker + """
> ## Superseded in part: the 2026-09-29 program update
>
> The deployed program was updated after this catalogue was frozen (layer commits `ce138ca` "whole-account
> liquidation, new asset set" and `64c4c84` "Oracle improvements for tokenized stocks"; layer HEAD `ecdb9b6`).
> The hosted fork runs it, and the MCP was ported to it. Where this document disagrees with the list below, **the
> list wins** (the sections below are kept for their unchanged material: envelope, math, health rule, dispatcher
> surface). Verified against the program source and live accounts.
>
> | Topic | Now | Was (in the sections below) |
> |---|---|---|
> | Interface file | `idl/vanna_lending.json` regenerated from the layer source with `cargo test --features idl-build` (SHA-256 `47cd9ced07539538e42e859b99ba535720a030371399a11ff732b6bc817ffe73`, LF); the backend's copy is stale | SHA `b438bfdd...` |
> | Pricing | Per-asset `OracleConfig` in `AssetConfig` (340-byte account): Kamino **Scope** chain first, **Pyth** push feed (x an optional second feed) as fallback when Scope is stale, x the klend cToken rate for Kamino receipts. Prices carry flags **fresh / TWAP ok / confidence ok** | Pyth `PriceUpdateV2` only, `price_source` enum |
> | Health-checked instructions | `user_borrow`, `user_withdraw_collateral`, `margin_execute` take **no price accounts**. `remaining_accounts` = (margin_execute: the CPI accounts, then) position groups `[asset_config, vault]` / `[asset_config, reserve, debt_position]` in slot order **without prices**, then the **oracle accounts** of every asset involved, each once, any order | Price accounts inside every group |
> | Price checks | Borrow, and withdraw / `margin_execute` while in debt, require **all** checks. A withdrawal with **no debt** reads no prices. Liquidation requires only a fresh price | Freshness + confidence only |
> | Liquidation | `public_liquidate()` has **no arguments**: whole-account, no close factor, no bonus, no health-improvement check; the liquidator repays every debt and receives every collateral at any HF <= 1.10 (`CollateralSeized` per collateral + `Liquidated`). Still **not** exposed for execution; `liquidation_snapshot` reports the whole-account model | 50% close factor, bonus, `max_repay_assets` / `min_collateral_out` |
> | Assets | Discovered on-chain (`getProgramAccounts`). Lending and borrowing pools: **SOL, USDC, USDT**. Collateral only: **JitoSOL, JupSOL, JupUSD, NVDAx, TSLAx** (xStocks are **Token-2022**) + Kamino receipts for **USDC and SOL** | 5 plain + 5 receipts, classic SPL only |
> | `dev-fork` oracle | Fabricate the Pyth feeds each asset pins and copy Kamino's live Scope account from mainnet, restamping the entries used to the fork clock (`surfnet_setAccount`) | Fabricate Pyth accounts only |
> | Product policy | The MCP only lends and borrows SOL / USDC / USDT (`asset_not_offered` otherwise); collateral-only assets still deposit, swap and farm | not stated |

"""
s = s.replace(marker, notice, 1)
p.write_text(s, encoding="utf-8")
print("ok")
