# HANDOFF - Vanna Solana MCP (updated 2026-09-29)

A Python MCP server for the Vanna Finance lending protocol on Solana (program `H37YVBG3...` on a hosted Surfpool
mainnet fork). **The MCP is built, ported to the current on-chain program, and verified live. The next
workstream is the Copilot.** Read `DECISIONS.md` (every decision, open items, the Copilot gate) then `CURRENT_STAGE.md`.

## Rules (from the owner)
- Only modify files inside this repo. Sibling repos are READ-ONLY references: `vanna-credit-layer` (Rust program =
  the authority), `vanna-credit-backend` (the Next.js app; the Copilot will live there), `Vanna_MCP/vanna-mcp` (Stellar MCP).
- The MCP never signs or broadcasts; every write returns an unsigned v0 envelope.
- Update the related docs in the same change as the code; decisions go in `DECISIONS.md`.
- Verify against the Rust source and live accounts; don't assume.

## State
- 14 dispatchers, layer HEAD `ecdb9b6` (D8). **204 tests pass live, 0 skipped** (87 of them offline).
- Git: branch `main`, remote `origin` = github.com/vannafinance/vanna_mcp_solana.
- Open items (none blocks the Copilot): O5 JupUSD/NVDAx/TSLAx product scope, O2 login provider and O3 hosting
  project (hosted deploy only), O4 per-user rate limits.
- Remaining Copilot gate item (app-side): one wallet-signed round trip through the app (sign -> broadcast ->
  `vanna_sign.tx_status`).

## Start here
```bash
cd /d/Projects/Vanna_MCP_Solana
./.venv/Scripts/python.exe -m pytest tests/test_foundation.py tests/test_pricing.py tests/test_health_accounts.py tests/test_hosting.py tests/test_price_keeper.py tests/test_read_cache.py -q   # 87 offline
./.venv/Scripts/python.exe handoff/program_probe.py     # program + ProtocolConfig + every AssetConfig/Reserve on the fork
./.venv/Scripts/python.exe handoff/price_check.py       # every asset's live price/source/age/flags + the keeper response
./.venv/Scripts/python.exe -m pytest tests/ -q          # full live suite (~7 min)
```
`handoff/`: `program_probe.py`, `rpc_probe.py`, `live_registry.py` (decode every AssetConfig), `price_check.py`,
`assemble_idl.py` (rebuild `idl/vanna_lending.json` from the layer source with `cargo test --features idl-build`).

## Next: the Copilot
`COPILOT_ARCHITECTURE.md` Part B is the plan (port of the Stellar Copilot into `vanna-credit-backend`; the MCP is its
backend). Work that needs nothing more from the MCP: the direct lane, router, firewall vocabulary and UI against the
frozen tool surface (`TOOLS_CATALOGUE.md` §3), and the app-side execution adapter reusing the existing wallet flow
(Part B.9). The Copilot can talk to a locally run MCP with `AUTH_ENABLED=false`.

## Pitfalls
- Bash heredocs containing quotes/apostrophes fail here; write scripts to a file and run them. The shell tool
  collapses `\\` to `\`, so build a backslash with `chr(92)` (this once wrote a NUL byte into `client.py`).
- The fork is flaky (502 / "Internal error") and does not support `getProgramAccounts`; the client retries transient
  errors and the registry falls back to known mints. Jupiter-route tests may skip on DEX-pool drift (none did on 2026-09-29).
- Session-scoped fixtures share one margin account (`live_margin_position`); use a fresh keypair for destructive tests.
- `tests/_live_helpers.sign_and_submit` uses raw JSON-RPC on purpose (solana-py panics on the fork's preflight error body).
- The backend's committed IDL and token list lag the program; use the layer source as the authority.
- Known gaps: temporary-ALT fallback for oversized txs is not built (`transaction_too_large` is returned); parity
  check against the backend's e2e scripts was skipped; liquidation execution (D5) and any Sign Service/Hermes (D3, D4)
  are out of scope by decision.
