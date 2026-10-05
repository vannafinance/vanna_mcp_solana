# Using the Vanna Solana MCP from Claude, Cursor or any MCP client

Everything the website and its Copilot do is available through this server, with the same guardrails. The split of work
is the Copilot's, with your MCP client in Gemini's place:

| | Website Copilot | MCP client (Claude, Cursor, ...) |
|---|---|---|
| Understand the sentence, choose the approach, ask a question, word the answer | Gemini | **the client's model** |
| Size amounts, check risk, refuse, apply limits, build and simulate every transaction | Copilot code + the MCP | **this server** (`vanna_plan`, `vanna_risk`, the safety layer) |
| Approve a plan, sign each transaction | the user's wallet | the user's wallet, on a page this server serves |

The server **never holds a key, never signs and never sends on its own.** There is no auto-sign. Every write comes back
unsigned; the user reviews and signs in their own wallet (Phantom, Backpack, Solflare).

---

## 1. Try it locally in Claude Code (nothing is deployed)

**Prerequisites:** Python 3.11+, this repo's `.venv` (`pip install -r requirements.txt -c constraints.txt && pip install -e .`),
a Solana wallet extension in your browser (Phantom, Backpack or Solflare), and Claude Code.

**1. Start the server** (leave it running):

```powershell
cd D:\Projects\Vanna_MCP_Solana
.venv\Scripts\python scripts\run_local.py
```

It listens on `http://127.0.0.1:8000` with the safety layer, the signing pages and the test faucet on, against the hosted
Surfpool fork. (`PORT=8765` to change the port; any value you set in the environment or `.env` wins over the defaults.)

**2. Add it to Claude Code** (once):

```powershell
claude mcp add --transport http vanna-solana http://127.0.0.1:8000/mcp
```

Open Claude Code and run `/mcp`: `vanna-solana` should be connected with 17 tools. Or skip step 1 and let Claude Code start
it for you over stdio (it serves the same pages on `127.0.0.1:8000`):

```powershell
claude mcp add vanna-solana -e SAFETY_LAYER=true -e ENABLE_SIGN_LINKS=true -e ENABLE_TEST_FAUCET=true -- D:\Projects\Vanna_MCP_Solana\.venv\Scripts\python.exe -m mcp_server.main
```

Cursor: `.cursor/mcp.json` in this repo uses the stdio form. Windsurf / others: same command in their MCP config.

**3. Talk to it.** Locally there is no login, so give your wallet address once (copy it from Phantom):

> My wallet is `<address>`. Put some test funds in it, then show me my portfolio.

Things to try, each exercising a different part:

| Say | What happens |
|---|---|
| "fund my wallet" | `vanna_wallet faucet`: 1,000 of each test token on the fork (3 times a day per wallet) |
| "what's my health factor / portfolio?" | `vanna_portfolio`, `vanna_account health` |
| "supply 100 USDC to Earn" | a single write: safety checks, then a **link**; open it, approve, sign, it confirms |
| "open a margin account and deposit 5 SOL" | a two-step plan with one approval and a signature per step |
| "lever my SOL 2x" | `vanna_plan lever`: sizes the borrow so health holds 1.15 even at a bad swap price, shows the worst case, then you approve |
| "how much can I safely borrow?" / "what if I withdraw 1 SOL?" | `vanna_risk capacity` / `what_if` |
| "close everything" | `vanna_plan unwind`: Kamino → repay → withdraw → Earn, in an order where health only improves |
| "swap SOL to get 100 USDC" | `vanna_plan swap_exact_out`: input sized from live quotes so the minimum received covers it |
| "borrow 99999 USDC" | refused by the server with the reason (floor / limits), whatever the model says |
| "set a stop-loss at 80" | refused: no conditional or standing orders |

Slash commands (MCP prompts): `/mcp__vanna-solana__lever`, `/mcp__vanna-solana__unwind`, `/mcp__vanna-solana__health_check`,
`/mcp__vanna-solana__earn_overview`, `/mcp__vanna-solana__vanna_assistant` (the full rules).

**What the signing page does** (`http://127.0.0.1:8000/plan/<id>`): shows the plan the *server* drew (summary, warnings,
worst-case health factor, each step) → you connect your wallet and sign an approval message that names the plan's
digest → for each step the server re-checks live data, builds and simulates it, and your wallet shows the real
transaction to sign → the server checks that what you signed is byte-identical to what it built, relays it, and watches
the confirmation. Claude then reports it from `vanna_plan action=status`.

**Phantom on a fork:** the page only asks your wallet to *sign*; the server broadcasts to the fork. Phantom may warn that it
cannot simulate the transaction or that it targets an unknown network; that is expected for a fork. Make sure the wallet
address you gave Claude is the account selected in the extension, or the page will refuse (it checks).

**Troubleshooting**
- *"No Solana wallet was found"*: the page needs the wallet extension in that browser profile; reload after installing.
- *`price_unavailable` / `has no registered AssetConfig`*: the fork's program doesn't match this server's pinned build
  (`PINNED_IDL_SHA256`, layer `ecdb9b6`). The server and the fork must run the same program version.
- *A step is "paused"*: the live health factor would have fallen below the floor since the plan was drawn; ask Claude for
  the status and redraw.
- Logs are on the server's console.

---

## 2. What the server enforces (so the model cannot talk its way past it)

With `SAFETY_LAYER=true` (always on in the public connector) every write is pre-flighted by the same code a plan step runs:

- **1.15 health-factor floor** on every action that can lower health (borrow, withdraw, margin swap); the program itself
  only enforces 1.10. Adding collateral and repaying are never blocked. Actions close to the floor are flagged.
- **Limits** per wallet: USD per action ($5,000) and per UTC day ($25,000), 6 transaction builds a minute, 8 steps a plan.
- **Amounts are exact**: negative, zero, `NaN`, scientific notation, ambiguous commas, or more decimals than the token has
  are refused, never rounded.
- **Asset policy**: only SOL/USDC/USDT lend and borrow; JitoSOL/JupSOL are collateral, swap and Kamino only.
- **Funding**: a plan is refused up front if the wallet cannot pay for its wallet-funded steps (0.05 SOL stays back for fees).
- **Exactly once**: each step is built, signed and relayed one time; an unclear outcome is `uncertain` and never retried;
  a transaction that never lands is rebuilt; two browsers cannot build the same step.
- **What was approved is what runs**: the plan is frozen with exact amounts and a digest; approval is a wallet signature
  over that digest; the signed bytes must equal the server's own build.
- **No custody**: only the user's wallet signs; the server verifies the signature and relays.

The model is also given the Copilot's firewall as instructions (domain limits, refusals for liquidation, admin, arbitrary
programs, perps/shorts, other liquidity, unknown tokens, other wallets, and conditional or standing orders; never invent a
number; never claim success before confirmation). Those refusals about *language* can only be instructions; the ones
about *money* are enforced above.

Defaults and limits are in `vanna_risk action=policy`.

---

## 3. The hosted public connector (anyone, with their own wallet)

Not deployed yet. The same server can run as `PUBLIC_MODE=true`: it is its own OAuth authorization server and **the wallet
is the login** (Sign-In-With-Solana), so a client adds one URL and the user connects a wallet once.

```bash
claude mcp add --transport http vanna-solana https://<public-mcp-host>/mcp
```

1. The client discovers the login and registers itself; the browser opens this service's own `/connect` page, which shows
   who is asking and where it will return, lets the user choose read-only or full access, and has the wallet sign a message
   the server issued.
2. After that every call acts as that wallet (defaults `authority`; any other wallet is refused with `wallet_mismatch`).
3. Writes work exactly as above: plan or sign link, approve and sign in the wallet.

Operator settings (the process refuses to start unless they are consistent):

| Variable | Meaning |
|---|---|
| `PUBLIC_MODE=true`, `AUTH_ENABLED=true` | required |
| `EXTERNAL_BASE_URL` | the public https URL; the token issuer and the base of every link |
| `OAUTH_SIGNING_KEY_PEM` | ES256 private key from Secret Manager; signs 15-minute access tokens |
| `OAUTH_CONSENT_URL` | optional; defaults to this service's own `/connect` |
| `TRUSTED_PROXY_HOPS=1` | Cloud Run's front end appends the client IP |

`FIRST_PARTY_CLIENT_IDS` must be empty. The state store is in-process for now: run **one instance** until the shared
(Firestore) store lands. The Copilot's own private service is untouched.

---

## 4. Settings reference

| Variable | Default | Meaning |
|---|---|---|
| `SAFETY_LAYER` | `false` (always on in public mode) | enforce the guardrails above on direct writes |
| `ENABLE_SIGN_LINKS` | `false` | serve the signing pages; writes return a link |
| `SIGN_BASE_URL` | `EXTERNAL_BASE_URL` or `http://127.0.0.1:PORT` | where the user's browser reaches the pages |
| `ENABLE_TEST_FAUCET` | `false` | `vanna_wallet action=faucet` (fork only) |
| `MIN_HEALTH_FACTOR` | `1.15` | the floor (must stay above 1.10) |
| `MAX_ACTION_USD` / `MAX_DAILY_USD` | `5000` / `25000` | per wallet |
| `WRITE_RATE_PER_MINUTE` | `6` | transaction builds per wallet |
| `FAUCET_REQUESTS_PER_WALLET_PER_DAY` | `3` | |
| `PLAN_APPROVAL_TTL_SECONDS` / `PLAN_RUN_WINDOW_SECONDS` | `300` / `1800` | approve within / keep running for |
| `MAX_PLAN_STEPS` | `8` | |
