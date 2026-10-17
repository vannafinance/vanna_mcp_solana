# Deploying the public connector (`vanna-mcp-solana-public`)

The public connector is the same image as the Copilot's private MCP (`vanna-mcp-solana`), run as a **separate Cloud Run
service** with `PUBLIC_MODE=true`. It is its own OAuth authorization server (the wallet is the login), serves the
connect and sign pages itself, and keeps all state in Firestore so any number of instances can serve any request.

| | Private (`vanna-mcp-solana`) | Public (`vanna-mcp-solana-public`) |
|---|---|---|
| Caller | the Copilot app (WorkOS machine token) | anyone's MCP client (Claude Code, Cursor, ...) |
| Cloud Run IAM | required (only the app may invoke) | none (`allUsers` invoker) |
| Login | WorkOS M2M | wallet signature (SIWS), tokens issued by this service |
| State | none needed | Firestore collection `mcp_state` |
| Signing key | none | ES256 key in Secret Manager |

Never give the private service these settings, and never set `FIRST_PARTY_CLIENT_IDS` on the public one (it refuses to
start). Settings are in `deploy/public-service.env.yaml`.

## One-time setup (project `solana-mcp`)

Run from any terminal logged in with `gcloud auth login`. Each step is safe to repeat.

```
# 1. The ES256 signing key, generated straight into Secret Manager (it never touches disk)
openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt | gcloud secrets create vanna-mcp-oauth-signing-key --project=solana-mcp --replication-policy=automatic --data-file=-

# 2. A runtime identity with only what it needs
gcloud iam service-accounts create vanna-mcp-public --project=solana-mcp --display-name="Vanna MCP public connector"
gcloud projects add-iam-policy-binding solana-mcp --member=serviceAccount:vanna-mcp-public@solana-mcp.iam.gserviceaccount.com --role=roles/datastore.user
gcloud secrets add-iam-policy-binding vanna-mcp-oauth-signing-key --project=solana-mcp --member=serviceAccount:vanna-mcp-public@solana-mcp.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor

# 3. Let Firestore delete expired records by itself (the server already ignores expired ones on read)
gcloud firestore fields ttls update expires_at --collection-group=mcp_state --enable-ttl --project=solana-mcp
```

Key rotation: add a new secret version, and put the old public key in `OAUTH_RETIRED_PUBLIC_KEYS_PEM` so tokens signed
before the change keep verifying until they expire (15 minutes).

## Build and deploy

```
gcloud builds submit . --project=solana-mcp --region=us-central1 --config=<build-only config> --substitutions=_TAG=<tag> --service-account=projects/solana-mcp/serviceAccounts/vanna-mcp-builder@solana-mcp.iam.gserviceaccount.com

gcloud run deploy vanna-mcp-solana-public --project=solana-mcp --region=us-central1 --image=us-central1-docker.pkg.dev/solana-mcp/vanna-credit/mcp-solana:<tag> --service-account=vanna-mcp-public@solana-mcp.iam.gserviceaccount.com --env-vars-file=deploy/public-service.env.yaml --set-secrets=OAUTH_SIGNING_KEY_PEM=vanna-mcp-oauth-signing-key:latest --allow-unauthenticated --ingress=all --min-instances=1 --max-instances=5 --timeout=300 --cpu-boost --quiet
```

The build-only config is the one the private service uses (`build` + `push`, `logging: CLOUD_LOGGING_ONLY`).
`EXTERNAL_BASE_URL` in the env file must be the https URL users reach the service on: it is the OAuth issuer, every
issued token names it, and the connect / sign pages are served on it. Today it is Cloud Run's own URL
(`https://vanna-mcp-solana-public-398807408840.us-central1.run.app`); it moves to the custom domain below once mapped.

## Approvals on the Vanna website (`APPROVE_PAGE_URL`)

Plan links can open on the website's approve page instead of this service's own `/plan/<id>` (which stays as the
fallback). Deploy the website with the page first (`MCP_PLAN_API_URL` = this service's origin, `PLAN_PROXY_SECRET` from
Secret Manager), then point the links at it:

```
gcloud run services update vanna-mcp-solana-public --project=solana-mcp --region=us-central1 --update-env-vars=APPROVE_PAGE_URL=https://test.solana.vanna.finance/approve
# and the shared secret, from Secret Manager, on both this service and the website: PLAN_PROXY_SECRET
```

The website's access gate applies to the page (a visitor without the code sees the gate first).

## Custom domain: `https://mcp.solana.vanna.finance` (pending)

Not live yet (2026-10-16): the gcloud account deploying the service has no verified domain, and the `vanna.finance`
zone sits in a project it cannot see, so a `vanna.finance` admin first makes that account a Search Console owner of
`vanna.finance` (Settings → Users and permissions → Manage property owners → Add an owner) and adds the CNAME of step 3.
Until then everything uses the run.app URL; nothing below has been applied.

The connector and its signing pages should live on a Vanna domain, so users (and wallets' phishing lists) can recognise
them. The site uses the same mechanism (`test.solana.vanna.finance` is a CNAME to `ghs.googlehosted.com`); `vanna.finance`
DNS is on Google Cloud DNS.

```
# 1. Once per account: the gcloud account must be a verified owner of vanna.finance (Search Console)
gcloud domains list-user-verified

# 2. Map the domain to the service (Cloud Run issues and renews the TLS certificate)
gcloud beta run domain-mappings create --service=vanna-mcp-solana-public --domain=mcp.solana.vanna.finance --project=solana-mcp --region=us-central1

# 3. The DNS record it asks for, in the vanna.finance zone
gcloud dns record-sets create mcp.solana.vanna.finance. --zone=<vanna.finance zone> --project=<DNS project> --type=CNAME --ttl=300 --rrdatas=ghs.googlehosted.com.

# 4. Wait until the certificate is ready (CertificateProvisioned = True), then check https://mcp.solana.vanna.finance/health
gcloud beta run domain-mappings describe --domain=mcp.solana.vanna.finance --project=solana-mcp --region=us-central1

# 5. Only then switch the issuer (no rebuild) and set the same value in deploy/public-service.env.yaml; run the smoke test on it
gcloud run services update vanna-mcp-solana-public --project=solana-mcp --region=us-central1 --update-env-vars=EXTERNAL_BASE_URL=https://mcp.solana.vanna.finance
```

Changing `EXTERNAL_BASE_URL` changes the OAuth issuer: tokens issued under the old URL stop verifying, so every client
reconnects once, on the new URL (`claude mcp remove vanna-solana`, then add it with the URL below). The run.app URL
keeps answering, but its OAuth metadata names the custom domain, so clients must use the custom domain.

After the domain is live, submit it to Phantom's and Blowfish's domain review so wallets show it as a known site.

On Cloud Run the service **refuses to start** unless `STATE_STORE=firestore`, `EXTERNAL_BASE_URL` is https, a signing key
is present, and no first-party client or dev-only fork tool is configured.

## Public access and the organization policy

`--allow-unauthenticated` adds an `allUsers` invoker. On 2026-10-05 the first deploy of `vanna-mcp-solana-public`
(revision `00001-d6v`, image `mcp-solana:c72e93e`) started cleanly, but granting that invoker failed with
`FAILED_PRECONDITION: One or more users named in the policy do not belong to a permitted customer`: the organization's
domain-restricted-sharing policy (`constraints/iam.allowedPolicyMemberDomains`) forbids `allUsers` in `solana-mcp`. Until the
service is publicly invokable, no MCP client can reach it.

**Resolved 2026-10-07:** the organization admin gave `solana-mcp` a project exception, and the `allUsers` invoker was added
with option 1 below. If the service is ever recreated in another project, the same choices apply, in this order of preference:

1. Give the project `solana-mcp` an exception to that constraint (Organization Policies, project override: allow all values),
   then `gcloud run services add-iam-policy-binding vanna-mcp-solana-public --project=solana-mcp --region=us-central1 --member=allUsers --role=roles/run.invoker`.
2. Run the public service in a project where public services are already allowed.
3. Cloud Run can also skip the invoker check (`--no-invoker-iam-check`), which exposes the service without an `allUsers`
   binding. That sidesteps the policy rather than satisfying it, so use it only with the organization admin's agreement.

## Verify

```
python scripts/smoke_public.py https://vanna-mcp-solana-public-398807408840.us-central1.run.app
```

It performs the whole path with a throwaway wallet (discovery, wallet-signature login, tool list, scoping, faucet, a
signed Earn supply, the safety refusals, a lever plan and its unwind, refresh-token rotation and reuse revocation) and
exits non-zero on the first failure. Run it once after every deploy.

Health checks use `/health`: Cloud Run's front end answers any path ending in `z` itself, so `/healthz` returns Google's
404 on the hosted service (it still works locally).

On the fork, a swap can fail simulation because a DEX pool's clone has drifted from mainnet; the swap builders re-clone
those pools and rebuild by themselves (`vanna_core/fork_heal.py`, logged as `fork heal: reset N stale DEX account(s)`).

Then connect a real client:

```
claude mcp add --transport http vanna-solana https://vanna-mcp-solana-public-398807408840.us-central1.run.app/mcp
```

Use `/mcp` in Claude Code to start the login; the browser opens the Vanna connect page, you approve with your wallet
(Phantom, Backpack or Solflare), and the client is connected. Transactions are then signed on the page each plan links to.

## Operating notes

- State is in the `mcp_state` collection: OAuth clients, pending logins, single-use codes, refresh-token families,
  revocations, plans, spend counters and rate-limit counters. Secrets (codes, refresh tokens) are stored only as SHA-256.
- Revoking a login: the client's own `/revoke`, or delete the family's documents (see `mcp_server/oauth/provider.py` for the key names); an access token lapses by itself after 15 minutes.
- Roll back by routing traffic to the previous revision; the state format is the same across revisions.
- A fork reset deletes the protocol lookup table: re-run `scripts/create_protocol_lookup_table.py` and update
  `PROTOCOL_LOOKUP_TABLES` in the env file and on the service. The current table is
  `zEEeoScWGrV1YeVzyUGar6S6KKiWcBLxeuYdwjoX5fg` (includes the Phoenix and Titan accounts).
- Per-wallet lookup tables (since the 2026-10-09 program upgrade): a wallet's first margin write returns a one-time
  setup transaction that creates its table. Its address is kept in the state store (`wallet_alt:<wallet>`) and named
  by a memo in the setup transaction, so any instance finds it again from the wallet's history after a restart.
- Every call into the app needs `APP_ACCESS_CODE` on the service (the app's access gate, backend `aaed5cc`); set it
  with `--update-env-vars` or a secret on both services, never in `deploy/public-service.env.yaml`.
- Predict trades are built by the app behind `PRICE_KEEPER_URL` (or `PREDICT_API_URL`): the hosted connector can only
  trade what the deployed app can build, so deploy the app before relying on a Predict change.
- Titan swaps need the `TITAN_API_KEY` secret (Secret Manager `titan-api-key`, mounted as the env var on both services).
  Without it the server still runs and quotes Jupiter only.
