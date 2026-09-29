# Vanna MCP — Solana

MCP server for the Vanna Finance lending protocol on Solana (`vanna_lending`,
program `H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC` on the hosted Surfpool
mainnet fork). Sibling to the Stellar/Soroban `vanna-mcp` server, adapted for a
single-program, no-registry protocol.

**The server never signs or broadcasts anything.** Every write dispatcher
returns an unsigned schema-v1 transaction envelope (`vanna_core/envelope.py`)
for the caller's own wallet to sign. The connected wallet signs, pays fees and
the app broadcasts; there is no Sign Service (`DECISIONS.md` D3).

## Docs

- `DECISIONS.md` — the team's decisions (cluster, assets, signing, pricing,
  liquidation, swap/farm), what each changed in the code, the open items, and
  the gate for starting the Copilot. **Read this first.**
- `TEAM_QUESTIONS.md` — the open questions for the team (options, recommendation, default), and what the
  Copilot needs from the repo owner.
- `CURRENT_STAGE.md` — what's built, what's deliberately out of scope, and
  how to test it.
- `HANDOFF.md` — the running session handoff and next steps.
- `TOOLS_CATALOGUE.md` — the frozen v1 tool surface, envelope schema, health
  and interest rules.
- `BUILD_PLAN.md` — the execution plan and status tracker.
- `COPILOT_ARCHITECTURE.md` — how the Copilot inside `vanna-credit-backend`
  uses this server.

References are `vanna-credit-layer` (program source, the authority) and
`vanna-credit-backend` (the Solana app). `Backend-Solana` is the old stack and
is not a reference.

## Structure

```
vanna_core/                    # protocol logic — no MCP/AI concerns
  config.py                     # env-driven settings (program ID, RPC URL, oracle mode, feature gates)
  assets.py                     # display catalogue, on-chain AssetConfig discovery, lend/borrow policy (SOL/USDC/USDT only)
  pdas.py                        # PDA derivation
  layouts.py                     # borsh account struct layouts (verified against live accounts)
  anchor_codec.py                # discriminator derivation (see "Why this isn't built on anchorpy")
  math_utils.py                  # WAD fixed-point math ported 1:1 from math/{interest,shares,health}.rs
  pricing.py                     # oracle facade ported 1:1 from oracle/*.rs (Scope chain, Pyth fallback, klend rate)
  oracle_reader.py               # batched fetch of the oracle accounts + chain clock, then pricing.get_price
  envelope.py                    # schema-v1 write envelope
  wsol.py                        # wallet-side ATA-create / SOL-wrap prefix instructions
  remaining_accounts.py          # health tail: position groups + oracle accounts (risk_engine.rs layout)
  margin_execute.py              # shared margin_execute (CPI-forwarding) instruction builder
  client.py                      # async RPC + v0-transaction build/simulate/envelope wrapper
  contracts/
    reserve.py, protocol.py, oracle.py, margin.py, margin_trade.py,
    wallet.py, history.py, kamino.py, farm.py, farm_kamino.py,
    jupiter.py, swap.py, fork.py, oracle_refresh.py (keeper / cheatcode price refresh)

mcp_server/                    # MCP transport + dispatcher registration
  dependencies.py                # VannaContext DI container (lazy-init contract wrappers, IDL hash check)
  error_handling.py              # @tool_safe decorator — maps exceptions to structured error dicts
  main.py                        # entrypoint: ScopedMCPServer (mcp>=2.2.0), stdio/streamable-http, /healthz, OAuth discovery
  auth.py, identity.py           # OAuth 2.1 resource server (JWKS bearer), caller identity per request
  scopes.py                      # read/write classification of every dispatcher action
  rate_limit.py                  # token-bucket rate limit + circuit breaker
  read_cache.py                  # 30 s cache for shared market reads
  agent_firewall.py              # instructions served to MCP clients
  dispatchers/                   # one file per MCP tool

idl/vanna_lending.json         # instruction IDL regenerated from vanna-credit-layer ecdb9b6 (pinned SHA-256, see below)
Dockerfile, cloudbuild.mcp.yaml # container + Cloud Build for the hosted MCP
tests/                          # see CURRENT_STAGE.md for coverage and how to run
```

## Why this isn't built on anchorpy

anchorpy 0.21.0's IDL parser cannot parse `idl/vanna_lending.json` — it uses
Anchor's newer explicit per-instruction/per-account `discriminator` array
format, and anchorpy-core (Rust) rejects it with
`SerdeJSONError: data did not match any variant of untagged enum
IdlAccountItem`. No anchorpy release compatible with this IDL was available.

Instead, `vanna_core/anchor_codec.py` recomputes Anchor's discriminators
directly (`sha256("global:<ix>")[:8]` / `sha256("account:<Struct>")[:8]`) —
cross-checked byte-for-byte against every discriminator the IDL itself
declares — and `vanna_core/layouts.py` hand-transcribes the account structs as
`borsh_construct` layouts, byte-size-verified against live on-chain accounts.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env     # defaults already point at the hosted fork
```

`.env.example` is fully commented — the only thing you'd normally change is
`ENABLE_FORK_TOOLS=true` if you want `vanna_fork` registered.

## Running the server

```bash
python -m mcp_server.main        # stdio transport (Claude Desktop / local dev)
```

Set `MCP_TRANSPORT=streamable-http` and `PORT` in `.env` to run it as an HTTP
server instead (`POST /mcp`, plus `/healthz`). For a hosted deployment set
`AUTH_ENABLED=true` with `OAUTH_ISSUER`, `OAUTH_JWKS_URI`, `OAUTH_AUDIENCE`
(and `FIRST_PARTY_CLIENT_IDS` for the Copilot); callers without write scope
only see and use read actions. The `Dockerfile` runs exactly this.

## Testing

```bash
pytest tests/ -v    # full suite; most files hit the live fork
pytest tests/test_foundation.py tests/test_pricing.py tests/test_health_accounts.py tests/test_hosting.py tests/test_price_keeper.py tests/test_read_cache.py -q   # offline only
```

See `CURRENT_STAGE.md` for the full test-file map, fixture notes, and known
flakiness.

## Keeping the IDL in sync

`idl/vanna_lending.json` is regenerated from the `vanna-credit-layer` program
source at `ecdb9b6` (`handoff/assemble_idl.py`), because the backend's copy
still describes the older program. The server refuses to start unless the
file hashes to `PINNED_IDL_SHA256` (`vanna_core/config.py`). When the program
changes: regenerate, verify against the deployed program, then update the pin.
The owner confirmed the fork runs this version (`DECISIONS.md` D8).
