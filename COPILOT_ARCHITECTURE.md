# Vanna Solana Copilot — Architecture

A chat assistant inside the Vanna Solana app. A user types things like *"what's my health factor?"*,
*"supply 100 USDC"* or *"lever my SOL 2x"*; the Copilot answers from live on-chain data and, for actions,
prepares the transactions for the user's own wallet to sign.

Editable board: [`docs/solana-copilot-architecture.excalidraw`](docs/solana-copilot-architecture.excalidraw)
(open in the Excalidraw extension, or import at [excalidraw.com](https://excalidraw.com)).

Status (2026-10-01): the **MCP server (this repo) is built and verified live** on the Solana fork. The
Copilot is **built** in `vanna-credit-backend` (branch `copilot`, not pushed). Hosting inventory in
`solana-mcp` and `vanna-credit-solana-test` is recorded in §11: Firestore exists, custom VPCs are
peered, the app runtime SA can call Vertex and Firestore. **Still needed to deploy:** WorkOS M2M
values (client id / secret / token URL / issuer / JWKS / audience were not pasted), MCP Cloud Run
image, and Direct VPC + Cloud NAT on the app service (not attached yet so the live site is unchanged).

**What it supports:** reads (health, positions, collateral, debt, wallet, prices, pool APYs, earned interest,
Kamino markets/positions, borrow/withdraw capacity, liquidation risk, history, swap quotes, protocol info) and
writes as one-step or multi-step plans (earn supply/redeem, margin open/close/deposit/withdraw/borrow/repay,
wallet and margin swaps, Kamino deposit/redeem, "lever my X 2x", chains of up to 8 steps). A single action is a
one-step plan, so it uses the same approval, exactly-once and confirmation path. **Not built:** unwinding
positions, conditional or standing orders, buy-exact-output swaps.

---

## 1. The idea in one picture

```mermaid
flowchart LR
  U["User in the browser<br/>(chat + wallet)"] -->|question / goal| C["Copilot<br/>(inside the app server)"]
  C -->|understand language| G["Gemini<br/>(Vertex AI)"]
  C -->|read data, build transactions| M["Vanna MCP<br/>(this repo)"]
  C -->|fast reads, broadcast| A["App's own Solana code<br/>(lib/solana)"]
  M --> F[("Solana fork<br/>Vanna program")]
  A --> F
  C -->|unsigned transaction| U
  U -->|wallet signs| C
  C -->|send + confirm| F
```

Three rules explain almost everything:

1. **The AI only understands language.** Gemini turns a sentence into an intent ("borrow", "USDC", "50").
   Plain code decides amounts, checks risk and refuses unsafe or unsupported requests.
2. **The MCP builds, the wallet signs.** Every transaction is built and simulated by the MCP and handed back
   *unsigned*. Only the user's connected wallet (Phantom, Backpack, Solflare) signs. No server holds a key.
3. **Nothing is "done" until the chain confirms it.** The Copilot never claims success from a built or signed
   transaction, only from a confirmed signature.

## 2. Is it the same as the Stellar Copilot?

**Same design, simpler signing.** It is a port of the Stellar Copilot's structure, not a new invention. Where
Solana differs, it is simpler:

| | Stellar Copilot | Solana Copilot |
|---|---|---|
| Where it runs | Inside the website's server | Same: inside `vanna-credit-backend`'s server |
| AI | Gemini on Vertex AI | Same |
| Tools | Vanna MCP (14 tools) | Same pattern: Vanna Solana MCP (14 tools) |
| Two lanes (quick answers vs. multi-step plans) | Yes | Same |
| Plans stored server-side, approved, run step by step | Yes | Same |
| Who signs | Wallet, **or** a server-side Sign Service with auto-sign | **Wallet only.** No Sign Service, no auto-sign |
| Login | Privy, WorkOS, or a Freighter wallet proof | **Sign-In-With-Solana** (wallet signs a login message) |
| DeFi integrations | Blend, Aquarius, Soroswap | Kamino (lend) and Jupiter (swaps) |
| Prices on the test network | Live oracle | The app's **price keeper** refreshes prices on the fork |

So everything to do with auto-sign, signer binding and the Sign Service is simply **not part of Solana**.

## 3. The pieces

| Piece | Where | What it does |
|---|---|---|
| **Copilot UI** | `vanna-credit-backend/components/copilot`, `/copilot` page | Chat, plan card, approval, step-by-step progress, asks the wallet to sign |
| **Copilot brain** | `vanna-credit-backend/lib/copilot` (server code) | Classifies the request, refuses off-topic or unsafe things, sizes amounts, keeps plans, calls the MCP |
| **Gemini** | Google Vertex AI in GCP project `solana-mcp`, model `gemini-3.8-flash` (location `global`) | Language understanding and answer wording only |
| **Plan and conversation store** | Firestore (a local encrypted file in development) | Keeps each plan and its state, so a step can never run twice |
| **Vanna MCP** | this repo, a separate service | 14 tools: reads (positions, prices, pools, history) and transaction builders (supply, borrow, swap, farm, ...). Never signs or sends |
| **App's Solana code** | `vanna-credit-backend/lib/solana` (already exists) | The same reads the app's pages use, plus the wallet send-and-confirm pipeline |
| **Price keeper** | the app's `POST /api/prices/refresh` (fork only) | Keeps oracle prices fresh on the test fork; the MCP calls it before any risk-checked build |
| **Chain** | hosted Surfpool mainnet fork, program `H37YVBG3…` | Where everything actually happens |

## 4. How a request flows

The Copilot has two lanes. A classifier picks one per message; a refusal (off-topic, liquidation, admin,
unknown token) always wins first.

### 4.1 A question — *"What's my health factor?"* (quick lane)

1. The Copilot knows the wallet from the login cookie.
2. It reads the position from the MCP (`vanna_account` `health`), which knows every asset registered on-chain
   and refuses to answer rather than leave a position out. (The app's `fetchMarginSnapshot` is not used for this
   yet: it only knows the app's five tokens and silently skips any other asset an account holds — see §8.)
3. Gemini words the answer from those numbers only. It never invents a number.

### 4.2 One action — *"Supply 100 USDC"* (quick lane)

1. Classifier: a single, fully specified write.
2. The Copilot calls the MCP tool `vanna_earn_write` `supply`. The MCP builds the transaction, **simulates it on
   the chain**, and returns it unsigned with the simulation result.
3. If the simulation failed, the Copilot shows the reason and stops. Otherwise it shows a summary and asks the
   user to confirm.
4. The wallet signs; the app sends it and waits for confirmation; then the Copilot reports the result.

### 4.3 A goal — *"Lever my SOL 2x"* (plan lane)

```mermaid
sequenceDiagram
  autonumber
  participant U as User + wallet
  participant C as Copilot
  participant M as MCP
  participant S as Plan store
  participant F as Chain
  U->>C: "lever my SOL 2x"
  C->>M: research reads (position, prices, max borrow)
  C-->>U: options (e.g. deposit X SOL, borrow Y USDC, swap to SOL)
  U->>C: pick an option
  C->>S: save plan with exact amounts (valid 5 min)
  U->>C: approve
  loop each step
    C->>M: fresh checks, then build this step
    M-->>C: unsigned transaction (simulated)
    C-->>U: please sign
    U->>C: signed transaction
    C->>F: send, wait for confirmation
    C->>S: step done (recorded once, never repeated)
  end
  C-->>U: summary with the final health factor
```

Rules the plan lane always follows:

- The browser only sends **ids, the approval, and signed transactions** — never amounts or instructions. The
  server holds the plan.
- An approval is tied to that exact plan and expires after **5 minutes**.
- Before each step it re-reads what may have moved (price, debt, pool liquidity, swap quote); if a step would
  push health below the **1.15** safety floor it pauses.
- Each step can be claimed only once, even with two servers running. An unclear outcome is marked
  **uncertain** and never retried automatically.
- A transaction not confirmed before its blockhash expires is marked **expired** and rebuilt.

## 5. Who does what

| Job | Done by |
|---|---|
| Understand the sentence | Gemini |
| Decide amounts, check risk, refuse | Copilot code (deterministic) |
| Position and health answers | MCP (`vanna_account`, `vanna_margin_status`) |
| Pool numbers, swap quotes, fresh checks between steps | App's `lib/solana` (same as the pages) or the MCP |
| Research reads for plans | MCP read tools |
| Build and simulate every transaction | **MCP** |
| Refresh fork prices before risky builds | MCP calls the app's price keeper |
| Sign | **User's wallet** (also pays the fee) |
| Send and confirm | App server (existing `getConnection`, `confirmSignatureHttp`) |
| Final receipt | `vanna_sign` `tx_status` + the app's history reader |

## 6. What the Copilot may and may not do

- **Assets:** exactly five tokens. SOL, USDC, USDT can be lent and borrowed; JitoSOL and JupSOL are collateral,
  swap and Kamino farm only; plus the Kamino receipts farming produces. Other assets the program registers
  on-chain (JupUSD, NVDAx, TSLAx) are not in the product: the MCP hides them and refuses them with
  `asset_not_offered`, while still counting any existing position in health and allowing its withdrawal.
- **Refused always:** liquidating someone, admin actions, unknown tokens, arbitrary programs, Kamino multi-asset
  liquidity, the test faucet.
- **Liquidation:** read-only — the Copilot can say whether an account is at risk, never execute a liquidation.
- **Health:** the protocol liquidates at 1.10; the Copilot sizes to stay at or above 1.15 (same as the app's
  "max" buttons).
- **The model never sees** transactions or signatures, only summaries.

## 7. How the Copilot talks to the MCP

- **Transport:** Streamable HTTP. `POST /mcp` with `initialize` → keep the `mcp-session-id` → `tools/call`.
  If the server answers **404** the session was lost (another instance); re-initialize and retry.
- **Tools (14):** `vanna_protocol_info`, `vanna_oracle`, `vanna_earn_market`, `vanna_earn_position`,
  `vanna_earn_write`, `vanna_account`, `vanna_margin_status`, `vanna_margin_trade`, `vanna_wallet`,
  `vanna_history`, `vanna_farm_overview`, `vanna_farm_kamino`, `vanna_swap`, `vanna_sign` (`tx_status` only).
  Full list of actions: `TOOLS_CATALOGUE.md` §3.
- **Write result (envelope):** `transactions[0].unsignedTransaction` (base64, v0), `requiredSigners`, `blockhash`,
  `lastValidBlockHeight`, `simulation_success`, `code`/`reason` when rejected, `summary`. **One transaction per
  action today.** Amounts are decimal strings.
- **Errors:** always `{"error": "<code>", "message": "..."}` — e.g. `invalid_input`, `asset_not_offered`,
  `price_unavailable`, `insufficient_scope`, `rate_limited` (HTTP 429 with `Retry-After`).
- **Auth (hosted):** a **WorkOS** machine-to-machine token for the Copilot app; listing its client id in
  `FIRST_PARTY_CLIENT_IDS` gives it write access. The MCP is deployed with **internal** ingress (not public).
  Locally the MCP can run without auth.
- **Why WorkOS (same as Stellar):** the MCP builds transactions for any wallet, so it must know *which application* is
  calling before it serves it. It verifies a WorkOS-signed token on every request
  (issuer, signature via the WorkOS key set, audience) and gives write access only to client ids listed in
  `FIRST_PARTY_CLIENT_IDS`. A caller without a valid token gets 401; other clients get write access only if their token carries the write scope.
  The Copilot is a server, so it uses WorkOS **machine-to-machine** (no user login): it posts `client_credentials`
  (client id + secret) to the WorkOS token URL, caches the returned token, refreshes it a minute before expiry, and
  sends it as `Authorization: Bearer` on every MCP call. End users never hold a WorkOS token: they prove who they are
  to the Copilot by signing a login message with their wallet.
- **Limits:** the Copilot enforces per-user request limits itself (all Copilot traffic reaches the MCP as one
  client), so no user can drive unlimited calls.

## 8. Verification — will this work?

Checked on 2026-09-30 against the real code of both repos and the live fork.

**Works today (verified):**

| Claim | Evidence |
|---|---|
| MCP reads and builds work on the current program | 211 live tests pass on the fork |
| The MCP works as a deployed service, as a client would call it | Docker image built and run; end-to-end HTTP test (initialize, session id, tools, errors, 404, 429) passes against it |
| App's `lib/solana` matches the current program | Backend commit `813fad3` moved it to the new oracle model (IDL with `OracleConfig`, oracle accounts in health checks); its own end-to-end script (`scripts/e2e-fork.ts`: Earn supply/redeem, margin create/deposit/borrow/repay/withdraw, history) passes on the fork, run 2026-09-30 |
| App's send-and-confirm and wallet pieces exist | `getActiveSigner`/`TxSigner`, `getConnection`, `confirmSignatureHttp`, `healStaleDexPools` in `lib/solana` |
| The checks a plan needs before each step exist in the app | `fetchMarginSnapshot`, `fetchPoolStates`, `fetchJupiterQuote`, `readReserveRate`, `maxBorrowUsd`, `projectHealthFactor` |
| Error text from the MCP fits the ported client | errors are short JSON the client can parse |

**Gaps to handle while building (none blocks starting):**

| Gap | Handling |
|---|---|
| The MCP's `preview` (before/after health factor) is not filled | The approval card gets the numbers from `vanna_margin_status` `can_borrow` / `can_withdraw` (or the MCP fills `preview` later) |
| The chain also registers JupUSD, NVDAx, TSLAx, which are not in the product | Out of scope by decision; the MCP hides and refuses them. A position in one can only exist if created outside the app; the app's `fetchMarginSnapshot` would silently skip it, so the Copilot takes position answers from the MCP, which counts it |
| All Copilot users share one MCP rate-limit bucket | Decided: the Copilot limits each user itself and backs off on 429 |
| Very large transactions (long swap routes) | The MCP returns `transaction_too_large`; the Copilot retries with a smaller amount. Lookup-table setup rounds are not built (not needed so far) |
| The fork's clock drifts and prices can go stale | The MCP refreshes (keeper + its own guarded supplement); a still-stale price makes the build fail safely |

**Decided (2026-09-30, hosting filled in 2026-10-01):** Gemini `gemini-3.8-flash` on Vertex in `solana-mcp`;
WorkOS for the Copilot-to-MCP token; MCP **internal** in `solana-mcp`; the app in
`vanna-credit-solana-test` reaches it over peered custom VPCs (`vanna-internal`). Limits in the
Copilot; product = the five tokens. **Still needed before a hosted launch:** the WorkOS M2M
credential itself (see §11), then deploy the MCP with `--ingress=internal` and attach Direct VPC
egress on the app (with Cloud NAT, so Jupiter and the fork RPC keep working).

**Verdict:** the architecture works with what exists. The only new code is the Copilot itself in the app.

## 9. Build order

1. **Read-only Copilot:** MCP client, wallet login (Sign-In-With-Solana), quick-lane answers, `/copilot` page.
2. **Single actions:** approval card, the wallet signs, the app sends and confirms. The first real signed round
   trip is the last open check before the Copilot is "ready".
3. **Plans:** plan store, research, step-by-step execution with fresh checks.
4. **Kamino and Jupiter strategies**, page assistant, then hosted deployment.

## 10. Decisions in force

| Topic | Decision |
|---|---|
| Network | Hosted Surfpool mainnet fork `https://rpc-devnet.solana.vanna.finance`, program `H37YVBG3eGtz89ofd7iDLc2PJgn12778392XeU2QJcGC` (vanna-credit-layer `ecdb9b6`) |
| Who builds transactions | The MCP (unsigned, simulated) |
| Who signs | The user's wallet; no Sign Service, no auto-sign |
| Prices on the fork | The app's price keeper (Jupiter prices); no Hermes this release |
| Lend/borrow | SOL, USDC, USDT only |
| Liquidation, admin, faucet | Not offered by the Copilot |
| Swaps and Kamino | Supported (Jupiter swaps; Kamino single-asset supply from the margin balance) |
| Product assets | SOL, USDC, USDT, JitoSOL, JupSOL (+ Kamino receipts). JupUSD / xStocks skipped for now |
| AI | Vertex Gemini `gemini-3.8-flash` in project `solana-mcp`; service account `vanna-copilot` (`roles/aiplatform.user`); no service-account keys (org policy), so hosted uses the attached account and local uses a developer's own login |
| Copilot -> MCP auth | WorkOS machine-to-machine token (see section 7). MCP ingress: **internal** in project `solana-mcp`. The app Cloud Run in `vanna-credit-solana-test` reaches it through peered custom VPCs named `vanna-internal` (see §11). WorkOS values themselves are not in the repo yet. |
| Limits | Per-user limits in the Copilot; no unlimited use |
| Mainnet | Not in scope now; everything targets the fork |

## 11. Hosting inventory (2026-10-01)

Checked with `anubhav@vanna.finance` as Owner on both GCP projects. Default VPCs in the two
projects both use `10.128.0.0/20` in `us-central1`, so they cannot be peered. Custom networks
were created instead. Direct VPC egress is **not** attached to the live app yet: doing that
with `all-traffic` without Cloud NAT would send Jupiter and fork-RPC through a subnet with no
internet path.

### App — `vanna-credit-solana-test`

| Item | Value |
|---|---|
| Cloud Run service | `vanna-credit-solana-test` in `us-central1` |
| Public origin | `https://test.solana.vanna.finance` (`NEXT_PUBLIC_SITE_URL`) |
| Run URL | `https://vanna-credit-solana-test-o3dmfat4aq-uc.a.run.app` |
| Runtime SA | `vanna-credit-runtime@vanna-credit-solana-test.iam.gserviceaccount.com` |
| Builder SA | `vanna-credit-builder@vanna-credit-solana-test.iam.gserviceaccount.com` |
| Current image | `us-central1-docker.pkg.dev/vanna-credit-solana-test/vanna-credit/app:813fad3` (app `main`, no Copilot env yet) |
| Custom VPC | `vanna-internal` / subnet `vanna-internal-usc1` `10.50.0.0/24` (Private Google Access on) |
| Peering | `app-to-solana-mcp` → `solana-mcp/vanna-internal` (**ACTIVE**) |

### MCP + Copilot data — `solana-mcp`

| Item | Value |
|---|---|
| Firestore | Native `(default)` in `us-central1` (created 2026-10-01; none existed before) |
| Vertex | `gemini-3.8-flash`, location `global`; SA `vanna-copilot@solana-mcp.iam.gserviceaccount.com` |
| Cloud Run | **Not deployed yet.** Target service name `vanna-mcp-solana`, `--ingress=internal` |
| Artifact Registry | `us-central1-docker.pkg.dev/solana-mcp/vanna-credit` |
| Custom VPC | `vanna-internal` / subnet `vanna-internal-usc1` `10.40.0.0/24` (Private Google Access on) |
| Peering | `solana-mcp-to-app` → `vanna-credit-solana-test/vanna-internal` (**ACTIVE**) |

Cross-project IAM on `solana-mcp` for the app runtime SA: `roles/datastore.user`,
`roles/aiplatform.user`, `roles/serviceusage.serviceUsageConsumer`. `roles/run.invoker` is
granted when the MCP service exists.

### Copilot env that hosting will set (no secrets in git)

```
MCP_BASE_URL=<internal Cloud Run URL>/mcp
MCP_AUTH_MODE=workos
GOOGLE_CLOUD_PROJECT=solana-mcp
GOOGLE_CLOUD_LOCATION=global
VERTEX_MODEL=gemini-3.8-flash
COPILOT_FIRESTORE_PROJECT=solana-mcp
COPILOT_FIRESTORE_DATABASE=(default)
COPILOT_FIRESTORE_COLLECTION_PREFIX=solana_copilot_
COPILOT_PUBLIC_ORIGIN=https://test.solana.vanna.finance
COPILOT_AUTH_DOMAIN=test.solana.vanna.finance
```

### Still needed from a person (cannot be read from GCP)

A **WorkOS machine-to-machine** credential for Copilot → MCP. A Dashboard **client id + API key**
(`sk_…`) is not the same thing as M2M `client_id` + `client_secret`. Paste in chat:

```
WORKOS_M2M_CLIENT_ID=
WORKOS_M2M_CLIENT_SECRET=
WORKOS_M2M_TOKEN_URL=
OAUTH_ISSUER=
OAUTH_JWKS_URI=
OAUTH_AUDIENCE=
FIRST_PARTY_CLIENT_IDS=
```

If only an AuthKit client id and `sk_…` API key exist, paste those and say so: the API key can
create an M2M application; it cannot be used as `WORKOS_M2M_CLIENT_SECRET`.

`COPILOT_SESSION_SECRET` / `COPILOT_STORE_SECRET` can be generated at deploy. Jupiter key and
price-keeper secret stay optional.

### Deploy-time remaining (after WorkOS)

1. Build and deploy this repo to `vanna-mcp-solana` in `solana-mcp` with `--ingress=internal`,
   Direct VPC on `vanna-internal` / `vanna-internal-usc1`, `AUTH_ENABLED=true`.
2. Grant the app runtime SA `roles/run.invoker` on that service.
3. Cloud NAT on the **app** subnet `vanna-internal-usc1`, then attach Direct VPC egress
   (`all-traffic`) to `vanna-credit-solana-test` so internal `*.run.app` is reachable without
   cutting off Jupiter and the fork RPC.
4. Set the Copilot env vars on the app service and deploy the `copilot` branch.
