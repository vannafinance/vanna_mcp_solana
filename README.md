# Vanna MCP — Solana

MCP server for the Vanna Finance lending protocol on Solana (`vanna_credit_layer`, with its oracle and validator agents,
program `H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC` on the hosted Surfpool
mainnet fork). Sibling to the Stellar/Soroban `vanna-mcp` server, adapted for a
single-program, no-registry protocol.

**The server never signs or broadcasts anything.** Every write dispatcher
returns an unsigned schema-v1 transaction envelope (`vanna_core/envelope.py`)
for the caller's own wallet to sign. The connected wallet signs, pays fees and
the app broadcasts; there is no Sign Service.

## Docs

- `TOOLS_CATALOGUE.md` — the frozen v1 tool surface, envelope schema, health
  and interest rules.
- `vanna-credit-backend/docs/copilot/COPILOT_ARCHITECTURE.md` — how the Copilot inside `vanna-credit-backend`
  uses this server.
- `docs/DEPLOY_PUBLIC.md` — deploying the public connector (`vanna-mcp-solana-public`): setup, deploy command, smoke test.
- `docs/CONNECTING_CLIENTS.md` — using this server from Claude, Cursor, Windsurf or any MCP client: the
  hosted connector (log in with your wallet) and local stdio, plus operator settings.
- `MCP_CONNECTOR_PLAN.md` — the plan and security model for the public connector (wallet-as-login OAuth).

References are `vanna-credit-layer` (program source, the authority) and
`vanna-credit-backend` (the Solana app). `Backend-Solana` is the old stack and
is not a reference.

## Structure

```
vanna_core/                    # protocol logic — no MCP/AI concerns
  config.py                     # env-driven settings (program ID, RPC URL, oracle mode, feature gates)
  assets.py                     # display catalogue, on-chain AssetConfig discovery, product scope (the app's token list), lend/borrow, deposit and farm rules
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
  margin_state.py                # the program's view of a margin: allowed-token collateral groups, venues, debts
  wallet_alt.py, protocol_alt.py # per-wallet lookup table (one-time setup tx) and the protocol-wide lookup table
  program_errors.py              # program error codes read from the IDL by name
  phoenix.py                     # Phoenix perps decoding (exchange, perp asset map, market session bits)
  vaults.py, vault_pricing.py    # the Basis / Perena vault catalogue and on-chain share pricing
  fork_heal.py                   # fork only: re-clone drifted DEX pool accounts before a swap rebuild
  app_gate.py                    # logs in to the app's access gate (APP_ACCESS_CODE) for every app call
  contracts/
    reserve.py, protocol.py, oracle.py, margin.py, margin_trade.py,
    wallet.py, history.py, kamino.py, farm.py, farm_kamino.py,
    jupiter.py, titan.py, swap.py, fork.py, oracle_refresh.py (keeper / cheatcode price refresh),
    perps.py (Phoenix), vaults.py (Basis / Perena), predict.py (World.xyz prediction markets, via the app)

mcp_server/                    # MCP transport + dispatcher registration
  dependencies.py                # VannaContext DI container (lazy-init contract wrappers, IDL hash check)
  error_handling.py              # @tool_safe decorator — maps exceptions to structured error dicts
  main.py                        # entrypoint: ScopedMCPServer (mcp>=2.2.0), stdio/streamable-http, /healthz, OAuth discovery
  auth.py, identity.py           # OAuth 2.1 resource server (JWKS bearer), caller identity per request
  oauth/                         # PUBLIC_MODE only: wallet-as-login OAuth authorization server (see below)
  planning/                      # the Copilot's brain as tools: risk facts, pre-flight, lever/unwind strategies, plan engine, guard
  safety.py                      # the safety layer: 1.15 floor gate, USD/day limits, build rate, faucet cap (counters in the record store)
  signing.py, web/               # signature checks + the browser pages where the user's wallet approves and signs
  prompts.py                     # MCP prompts (/mcp__vanna-solana__lever ...)
  wallet_scope.py                # public logins may only act as their own wallet (forced on every write)
  scopes.py                      # read/write classification of every dispatcher action
  rate_limit.py                  # token-bucket rate limit + circuit breaker
  read_cache.py                  # 30 s cache for shared market reads
  agent_firewall.py              # instructions served to MCP clients
  dispatchers/                   # one file per MCP tool

idl/vanna_credit_layer.json    # instruction IDL of the vanna_credit_layer program (layer 465f21a; pinned SHA-256, see below)
Dockerfile, cloudbuild.mcp.yaml # container + Cloud Build for the hosted MCP
tests/                          # see the Testing section below
```

## Why this isn't built on anchorpy

anchorpy 0.21.0's IDL parser cannot parse `idl/vanna_credit_layer.json` — it uses
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

At startup the asset registry is loaded in the background, so the first request doesn't pay for it. Asset discovery
tries `getProgramAccounts` once, with no retry backoff. An RPC that refuses it (the hosted fork does) gets the exact
known-mints discovery straight away and is asked again hourly. On the fork this took a cold margin build from ~34 s
to ~6 s.

Every tool's network calls share two layers. On the fork, a new HTTPS connection costs ~1.2-1.7 s and a request on a
kept one ~0.3 s:
- **Connections are kept.** The RPC client keeps idle connections for 30 s instead of httpx's 5 s. Every other outbound
  call (raw RPC posts such as Phoenix view simulations, the app's callbacks, Jupiter, Titan, Phoenix and Perena) uses
  one shared pool (`vanna_core/http_pool.py` `pooled_http`) instead of a new client per call.
- **Repeats are shared** (`vanna_core/client.py` `_ResilientRpc`). Identical read calls in flight send one request. Three
  answers are reused briefly, only where that can't mislead: the recent blockhash for 5 s (valid ~60 s), the clock
  sysvar for 1 s, and the rent-exempt minimum always. Account data and simulations are never reused across time.

The vault list went from ~27-32 s to ~11-14 s from a cold process.

## Using it from Claude, Cursor or any MCP client

The MCP client's model replaces the Copilot's Gemini (understanding, planning, wording); this server does everything that
must be exact — sizing, risk checks, limits, building and simulating transactions, approval and exactly-once execution —
with the Copilot's guardrails. 20 dispatcher tools: reads, `vanna_plan` (lever, unwind, exact-output swap, custom chains),
`vanna_risk` (capacity, what-if, leverage, liquidation distance), `vanna_portfolio`, the original write tools, Phoenix
perps (`vanna_perps`), Basis / Perena vaults (`vanna_vaults`), World.xyz prediction markets (`vanna_predict`), and a
test faucet. Writes return a link where the user's own wallet approves and signs; the server never signs.

```powershell
.venv\Scripts\python scripts\run_local.py
claude mcp add --transport http vanna-solana http://127.0.0.1:8000/mcp
```

Full local walkthrough, what is enforced, and settings: `docs/CONNECTING_CLIENTS.md`.

## Public connector (any MCP client)

Besides the Copilot's private service, the same image can run as a **public connector** (`PUBLIC_MODE=true`)
that anyone can add to Claude, Cursor or another MCP client. There is no separate account: the user's
**wallet is the login**.

- The process is its own OAuth 2.1 authorization server (`mcp_server/oauth/`), built on the MCP SDK's
  authorization-server toolkit: dynamic client registration, PKCE (S256 only), refresh rotation, revocation.
- `/authorize` parks the request and sends the browser to the Vanna app's consent page (`OAUTH_CONSENT_URL`),
  which is UI only. The **server** builds the Sign-In-With-Solana message, verifies the wallet's Ed25519
  signature, and only then mints a single-use authorization code. Tokens are 15-minute ES256 JWTs bound to
  this server (`aud`) whose subject is the wallet.
- Every call then acts as that wallet (`wallet_scope.py`): a missing `authority`/`owner` defaults to it, and a
  write that names any other wallet is refused with `wallet_mismatch`. Reads of public addresses stay open.
- Nothing is ever signed server-side. Writes return the same unsigned envelope as today; the user signs in
  their own wallet. No auto-sign, no Sign Service.
- The Copilot's service is untouched (IAM-locked, WorkOS machine-to-machine). Public mode refuses to start
  alongside `FIRST_PARTY_CLIENT_IDS` or the fork tools.

Status: live on the test fork since 2026-10-07 as the Cloud Run service `vanna-mcp-solana-public`
(https://vanna-mcp-solana-public-398807408840.us-central1.run.app/mcp). It serves its own connect page and the
signing pages, and keeps its state in Firestore (`mcp_state`). Deploy steps: `docs/DEPLOY_PUBLIC.md`; operator
settings: `docs/CONNECTING_CLIENTS.md` §3.

Signing pages (`/plan/<id>`): a direct write is a one-step plan with its own link, vault, perps and Predict writes
included (they are `raw` plan steps). A Predict trade is several transactions signed in **one** wallet approval
(`signAllTransactions`); the server then sends them in order, each confirmed before the next, and merges the pool
after the last one lands.

## Deploying

- **Reproducible build:** the `Dockerfile` installs `requirements.txt` under `constraints.txt`, the exact
  package set the image was built and tested with. To upgrade a dependency, change it there, rebuild, and
  rerun the live suite plus the container test below.
- **Cloud Run:** MCP hosts in GCP project `solana-mcp`, region `us-central1`, Artifact Registry
  `vanna-credit`, service name `vanna-mcp-solana`. Ingress is `all`, but **Cloud Run IAM is required**:
  only the Copilot app's runtime service account (`vanna-credit-solana-test` project) holds
  `roles/run.invoker`, and the MCP also verifies the WorkOS token. The app sends the Google ID token in
  `X-Serverless-Authorization` and the WorkOS bearer in `Authorization`. (`--ingress=internal` was tried
  first and does not work: a caller from another project gets a 404 from Cloud Run.) Do not add an
  `allUsers` invoker. `cloudbuild.mcp.yaml` builds, pushes and deploys. MCP sessions
  (`mcp-session-id`) live in instance memory, so the deploy sets `--session-affinity` and
  `--min-instances=1`; a request that still reaches another instance gets 404 and the client
  re-initializes. Runtime settings (`AUTH_ENABLED`, `OAUTH_*`, `EXTERNAL_BASE_URL`,
  `SOLANA_RPC_URL`, `JUPITER_API_KEY`, ...) are set on the service. Both services also need
  `APP_ACCESS_CODE` (the app's site-wide access code, backend `aaed5cc`): without it every call into the app
  (price keeper, Jupiter proxy, Phoenix helpers, vault APY, faucet, Predict) gets 401. Set it with
  `--update-env-vars` or a secret, never in git. With `AUTH_ENABLED=true` the server
  refuses to start unless `OAUTH_ISSUER`, `OAUTH_JWKS_URI` and `OAUTH_AUDIENCE` are set. Set
  `TRUSTED_PROXY_HOPS=1` (Cloud Run's front end appends the client IP; the hosted service sets it).
  Concrete project values: `vanna-credit-backend/docs/copilot/COPILOT_ARCHITECTURE.md` §11.
- **Product scope:** the tokens the app lists: SOL, USDC, USDT, JitoSOL, JupSOL, JupUSD and the Token-2022 xStocks
  (NVDAx, GOOGLx, AAPLx, TSLAx), plus the Kamino receipts. Only SOL, USDC and USDT are lent and borrowed; the rest are
  margin collateral and swap only. As on the website, USDT is not a margin collateral deposit (`assert_depositable`)
  and the JitoSOL/JupSOL Kamino farms are closed to new supply, redeem still allowed (`assert_farm_listed`). A
  registered asset outside the list, and the GMTrade venue, are refused with `asset_not_offered`.
- **Heap frame:** the core program's allocator (vanna-credit-layer `src/heap.rs`) can use 256 KiB, so every transaction
  that calls the core requests a 256 KiB heap frame (`vanna_core/client.py` `compute_budget_prefix`), as the app does.
- **Margin swaps and transaction size:** a margin swap is the Jupiter route plus the margin instruction's
  own accounts, which can exceed Solana's 1232-byte limit (always for routes into JupSOL/JitoSOL before this
  fix). `margin_swap` checks the compiled size and re-quotes with a tighter Jupiter `maxAccounts` until it
  fits, and loads the protocol-wide accounts from a frozen lookup table named by `PROTOCOL_LOOKUP_TABLES`.
  Create that table once per cluster with `python scripts/create_protocol_lookup_table.py` (a fork reset
  deletes it; builds then skip it). Only margin swaps use it: accounts loaded from a lookup table are not
  indexed by the fork's `getSignaturesForAddress`, which Earn history relies on.
- **Program upgrade of 2026-10-09** (vanna-credit-backend `2462473`, the fork's core rebuilt from
  `feature/phoenix-titan`): token collateral is the protocol's `ProtocolConfig.allowed_tokens` list (23 tokens), scanned
  for every margin; a margin keeps only its venues. `vanna_core/margin_state.py` joins the two the way the program does,
  and every health-checked write names a group for each allowed token, then venues, then debts. Those 23 per-wallet
  vault keys don't fit next to a big instruction, so a wallet gets its own lookup table (`vanna_core/wallet_alt.py`):
  a write that doesn't fit returns a one-time setup transaction first (`summary.prerequisite`), then the action. Events
  are CPI events now (`client.get_transaction_event_payloads`). Error codes are read from the IDL by name
  (`vanna_core/program_errors.py`).
- **Vaults:** `vanna_vaults` (Basis delta-neutral SOL/ETH/BTC/TSLA/META, Perena USD* / USD* Junior) ports the app's
  `vault-actions.ts` / `vault-state.ts` / `perena.ts`; vault shares are priced like the program does
  (`vanna_core/vault_pricing.py`).
- **Predict:** `vanna_predict` (World.xyz prediction markets) calls the app's `/api/predict/*`: the app builds each
  trade and its maker co-signs it; the MCP checks the batch (payer, co-signer, programs) and the wallet signs every
  transaction in one approval. Every app call goes through the app's access gate with `APP_ACCESS_CODE`.
  `PREDICT_API_URL` points the Predict calls at a different app than `PRICE_KEEPER_URL` (locally:
  `http://localhost:3000/api/predict`, so trades are built by the app version under test).
- **Calls into the app** (all through `vanna_core/app_gate.py`): `POST /api/prices/refresh` (keeper), `/api/jupiter/*`
  (Jupiter with the app's key), `POST /api/perps/phoenix-book` and `/phoenix-access` (fork helpers), `GET /api/farm/vaults`
  (vault APY rows), `POST /api/faucet` and `/api/predict/*`. Titan is called directly with the MCP's own `TITAN_API_KEY`.
- **Before a deploy, test the image itself:**

  ```bash
  docker build -t vanna-mcp-solana:local .
  docker run -d --name vmcp -p 18080:8080 -e PORT=8080 -e AUTH_ENABLED=false vanna-mcp-solana:local
  MCP_E2E_URL=http://127.0.0.1:18080 pytest tests/test_http_e2e.py -q
  ```

## Testing

```bash
pytest tests/ -v    # full suite; most files hit the live fork
pytest tests/test_foundation.py tests/test_pricing.py tests/test_health_accounts.py tests/test_hosting.py tests/test_price_keeper.py tests/test_read_cache.py -q   # offline only
pytest tests/test_oauth_connector.py tests/test_wallet_scope.py tests/test_connector_sdk_client.py tests/test_risk_math.py tests/test_planning.py tests/test_web_and_dispatch.py -q   # connector, planner, safety layer, signing pages: offline (no RPC)
```

`tests/test_http_e2e.py` starts the real server process and talks raw Streamable HTTP to it, as a Copilot
client does (initialize, session id, `tools/list`, `tools/call`), cross-checking answers against direct
on-chain reads; set `MCP_E2E_URL` to run it against a container or a deployed service instead.

Live tests (one file per dispatcher, plus `test_margin_composites.py`, `test_token2022_collateral.py`
and `test_oracle_refresh.py`) create real accounts on the fork with disposable keypairs; the MCP itself
never signs, only `tests/_live_helpers.py` does. `tests/conftest.py`'s `live_margin_position` is a shared,
session-scoped position: never drain it in a new test. Jupiter-route tests may skip when the fork's cloned
DEX pools drift from mainnet.

## Keeping the IDL in sync

`idl/vanna_credit_layer.json` is the IDL of the `vanna_credit_layer` program (layer `465f21a`), copied from
`vanna-credit-backend/idl/` (the previous `vanna_lending` IDL is kept in `handoff/old/`). The server refuses to start unless the
file hashes to `PINNED_IDL_SHA256` (`vanna_core/config.py`). When the program
changes: regenerate, verify against the deployed program, then update the pin.
