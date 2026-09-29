# Questions for the team — Vanna Solana MCP and Copilot

Written 2026-09-29. The MCP is finished and verified live (204 tests, `CURRENT_STAGE.md`); the next
workstream is the Copilot, which lives in `vanna-credit-backend`. These are the questions the work
needs answered, in priority order. Each one says what it blocks, the options, what I recommend, and
the **default I will use if nobody answers**, so nothing waits on a reply unless it says so.

Answers go into `DECISIONS.md` (the ids in brackets, O2–O9, are the open items there).

| # | Question | Blocks | Owner to answer |
|---|---|---|---|
| Q1 (O6) | The backend's `lib/solana` targets the *old* program. Who ports it, and how does the Copilot read positions meanwhile? | The Copilot's reads | Backend owner |
| Q2 (O7) | Which Google Cloud project, Vertex AI models and Firestore database does the Copilot use? | Running the Copilot at all | Whoever owns GCP |
| Q3 (O2) | Which service issues the machine token the Copilot sends to the MCP? | Hosted MCP only | Platform / infra |
| Q4 (O3) | Which GCP project, region and service account host the MCP? | Hosted MCP only | Platform / infra |
| Q5 (O8) | The price keeper is unauthenticated and skips JupUSD. Harden and extend it, or keep the MCP's workaround? | Public exposure; JupUSD freshness | Backend owner |
| Q6 (O5) | Are JupUSD, NVDAx and TSLAx product scope for the Copilot? | Nothing (default set) | Product |
| Q7 (O4) | How should per-user rate limits work behind the Copilot's single machine token? | Nothing (default set) | Backend / MCP |
| Q8 (O9) | When and where is the launch cluster (mainnet)? | Production oracle plan | Product / engineering lead |

## Needed from the repo owner (not the team)

- **Permission to write in `vanna-credit-backend`.** Every rule so far confined my changes to this
  repo. The Copilot has to be built inside `vanna-credit-backend` (`COPILOT_ARCHITECTURE.md` B.0), so I
  need explicit approval, ideally to work on a **new branch** (for example `copilot`) rather than
  `main`. Default without an answer: I do not touch that repo and only prepare code and docs here.
- **Whether to push this repo to GitHub.** Everything is committed locally on `main`
  (`origin` = `github.com/vannafinance/vanna_mcp_solana`); nothing has been pushed.

---

## Q1 (O6) — The backend's `lib/solana` still targets the old program

**What I found (2026-09-29, checked in the code).** `vanna-credit-backend` was last updated for the
older oracle model. Its `idl/vanna_lending.json` has the old `AssetConfig` (`price_feed_id`,
`price_source`; 258 bytes) and `user_borrow` still lists a `price_update` account. `lib/solana/margin.ts`
(`fetchMarginSnapshot`, the borrow/withdraw builders), `lib/solana/exec.ts` (`margin_execute` with
`spentPriceUpdate`/`receivedPriceUpdate`) and `lib/solana/protocol-state.ts` (`fetchAllAssetConfigs`)
are all written to that layout, and `lib/solana/tokens.ts` lists five assets. The fork now runs layer
HEAD `ecdb9b6`: a 340-byte `AssetConfig` with an `OracleConfig`, no price accounts on borrow, withdraw
or `margin_execute`, whole-account liquidation, and 13 registered assets. So the app's own margin page
and its borrow/withdraw calls probably fail on the current fork. I have not run the app to confirm.

**Why it matters for the Copilot.** `COPILOT_ARCHITECTURE.md` B.1/B.5 plans the Copilot's
position answers on `fetchMarginSnapshot`, "so chat and page can never disagree". That function cannot decode
the current accounts, so the plan as written does not work until `lib/solana` is ported.

**Options.**
1. **Copilot reads through the MCP now; the app port is tracked separately (recommended).** The MCP
   already decodes the current program and is verified live: `vanna_account.health`,
   `vanna_margin_status`, `vanna_oracle`, `vanna_earn_*`, `vanna_wallet`. One source of truth, no
   dependency on the port. Cost: one extra network hop per read, and chat and page can disagree until
   the page is ported.
2. **Port `lib/solana` first** (IDL, `AssetConfig`/`OracleConfig` decode, price-less health tail, the
   Scope-first oracle rules, the 13-asset token list). The right end state for the app pages, but it
   is app work, and it puts the Copilot behind it.
3. **Port only what the Copilot needs** (a read-only subset). Same risk of two implementations
   drifting; I do not recommend it.

**Recommendation.** Option 1 for the Copilot, and a separate decision on who ports the app pages.

**Needed.** Who owns the `lib/solana` port and roughly when. **Default:** Option 1.

## Q2 (O7) — Google Cloud, Vertex AI and Firestore for the Copilot

**Context.** The Stellar Copilot (`mercury-stellar-backend/lib/copilot`) calls **Vertex AI Gemini**
(default `gemini-3.7-flash`, a lighter model for small talk, and a fallback list) on GCP project
`vanna-mcp` with location `global`, authenticating with a service-account key or workload identity. It
stores conversations and workflow records in **Firestore** (one document per conversation, encrypted
with a session secret). The Solana app deploys to a different project (`vanna-credit-solana-test`,
`us-central1`, `cloudbuild.yaml`). Neither Vertex nor Firestore is configured for the Solana app.

**Needed.**
1. The GCP project for Vertex AI, and confirmation that its service account has Vertex access.
2. The Gemini models to use (same as Stellar, or something else) and any spend cap.
3. A Firestore project and database for workflows and conversations (the architecture proposes
   `copilot-workflows-solana`), and the runtime service account's access (`roles/datastore.user`).
4. Two secrets: `COPILOT_SESSION_SECRET` (signs the wallet session cookie) and `COPILOT_RESEARCH_SECRET`.

**Default.** Development uses a local encrypted journal instead of Firestore (the Stellar code has this
mode) and Vertex credentials from a developer's own environment. I can build and test the whole
read-only Copilot that way; only the hosted deploy needs the real project.

## Q3 (O2) — Who issues the Copilot's machine token for the MCP?

**Context.** The hosted MCP is an OAuth 2.1 resource server (`mcp_server/auth.py`): it needs an issuer, a
JWKS URI and an audience, and it trusts a small set of first-party client ids. Stellar used a WorkOS
machine-to-machine token. The Solana app has no Privy or WorkOS; its only identity today is the
connected wallet.

**Options.**
1. **Google-signed service-to-service ID tokens (recommended).** Both the app and the MCP run on Cloud
   Run, and Cloud Run can mint an ID token for one service to call another. The MCP verifies it against
   Google's public keys with the MCP's URL as audience. No new vendor or secret.
2. **WorkOS M2M**, as Stellar does. Familiar, but a new vendor for this stack.
3. **A shared bearer secret.** Simplest, but rotation and leakage risk. Acceptable for development only.

**Needed.** The choice, plus issuer, JWKS URI and audience if it is not option 1. **Default:** no auth
(`AUTH_ENABLED=false`) for local and staging-behind-VPC work; the Copilot talks to a locally run MCP.

## Q4 (O3) — Where does the MCP run?

**Context.** `Dockerfile` and `cloudbuild.mcp.yaml` are ready, with placeholders (`_SERVICE=vanna-mcp-solana`,
`_IMAGE_NAME=mcp-solana`, `_REPO=vanna-credit`). The Docker image has not been built on any machine yet.

**Needed.** GCP project, region, Cloud Run service name, runtime service account, whether it should be
publicly reachable or internal-only, minimum instance count, and the URL that becomes `MCP_BASE_URL` in
the app. **Recommendation:** the same project and region as the app (`vanna-credit-solana-test`,
`us-central1`), internal-only ingress, and one warm instance or session affinity (MCP sessions, the `mcp-session-id`
header, are held in memory per instance, so a request landing on a different instance would not find its session).
**Default:** none; nothing deploys until this is answered.

## Q5 (O8) — Price keeper: authentication and JupUSD

**Context.** The app's `POST /api/prices/refresh` writes the fork's oracle accounts, and `/api/faucet`
mints test tokens. Both are unauthenticated today (`COPILOT_ARCHITECTURE.md` B.11). The MCP calls the
refresh before every health-checked write. I also found live that the keeper refreshes only the SOL,
USDC, USDT and JupSOL-rate Pyth feeds; **JupUSD has only a Pyth feed** (no Scope chain), so without a
writer it goes stale after 180 s and every borrow or in-debt withdrawal on an account holding JupUSD
fails.

**Options for JupUSD.** (a) Add the JupUSD feed to the keeper. (b) Keep the MCP's own small supplement,
which writes only Pyth-only feeds the keeper did not write (shipped today).
**For authentication.** Set `PRICE_KEEPER_SECRET` on the app (the MCP already sends it as a bearer),
and disable both endpoints outside a fork.

**Recommendation.** Secret plus disabling outside the fork before anything is public; (a) if JupUSD is
in scope (Q6), otherwise leave the supplement. **Default:** keep the supplement, no change to the app.

## Q6 (O5) — Are JupUSD, NVDAx and TSLAx product scope?

**Context.** On the fork these three are registered collateral-only assets (no pool, not borrowable).
NVDAx and TSLAx are Token-2022 xStocks priced from Scope. The app's token list, and the team's asset
answer (D2), name only SOL/USDC/USDT/JitoSOL/JupSOL. The MCP supports all three (deposit, valuation and
withdraw are verified live for NVDAx and TSLAx); swaps and the Kamino farm for them are untested and the app does not offer them.

**Needed.** Expose them to the Copilot, or filter them out (a one-line allowlist in
`vanna_core/assets.py`)? **Default:** expose them as read-only collateral in the Copilot's vocabulary, and
do not offer swaps or farming for them.

## Q7 (O4) — Per-user rate limits behind one machine token

**Context.** The MCP's rate limit is keyed by the token's subject, then client id, then IP. Each request
draws tokens from that key's bucket (a write costs 10, a heavy read 5, a read 1; the bucket refills 8 per
second with a burst of 24 by default). The Copilot calls with one first-party machine token, so all Copilot
users share one bucket.

**Options.** (1) The Copilot enforces per-user limits itself and the MCP treats the Copilot as one
client (default, needs no change). (2) The Copilot forwards a stable user key (the wallet address) in a
header the MCP trusts only from first-party tokens, and the MCP keys buckets on it.

**Default:** option 1. Raise it if one busy user can starve the rest.

## Q8 (O9) — Launch cluster and timeline

**Context.** Everything targets the hosted Surfpool fork. The fork has been restarted empty once
(2026-09-29) and needed a redeploy and bootstrap. The launch cluster is still undecided
(`COPILOT_ARCHITECTURE.md` B.15, item 1). On a real cluster the fork-only pieces disappear (the price
keeper, the cheatcode writer, the faucet) and a real oracle plan is needed (Hermes credentials or a Pyth
pull adapter, which was deliberately dropped for this release).

**Needed.** Whether and when a mainnet launch is planned, and who owns the redeploy-and-bootstrap
runbook for the fork in the meantime. **Default:** stay on the fork; the MCP refuses to start if the IDL
hash differs from the pinned one, so a program change is noticed immediately.

---

## Defaults the Copilot will use unless told otherwise

- **Identity:** Sign-In-With-Solana wallet session (a signed challenge, a sealed cookie), no Privy.
  Every write is wallet-signed; there is no auto-sign (D3).
- **Network gate:** the configured cluster label (`surfpool-fork`).
- **Investigate-first:** on, as on the Stellar Copilot.
- **Health floor:** the Copilot's sizing keeps health factor at or above 1.15 (`SAFE_HEALTH_FACTOR`);
  the protocol's own threshold is 1.10 and comes from the program, never from this constant.
- **Refusals:** liquidation execution, admin actions, unknown mints, arbitrary program calls, Kamino
  multi-asset liquidity, and the faucet are refused by the domain firewall.
