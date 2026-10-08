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
`EXTERNAL_BASE_URL` in the env file must be the service's own https URL (Cloud Run's is
`https://<service>-<project number>.<region>.run.app`); a custom domain means changing it and redeploying, because it is
the OAuth issuer and every issued token names it.

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
  `5gCxkk3VFCE96Ueej8sc6bm3wjJahBPvgw4ewaknSF74` (includes the Phoenix and Titan accounts).
- Titan swaps need the `TITAN_API_KEY` secret (Secret Manager `titan-api-key`, mounted as the env var on both services).
  Without it the server still runs and quotes Jupiter only.
