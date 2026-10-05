"""End-to-end check of a running PUBLIC connector (local or deployed), the way a real client uses it.

    python scripts/smoke_public.py https://vanna-mcp-solana-public-398807408840.us-central1.run.app
    python scripts/smoke_public.py http://127.0.0.1:8770          # a local PUBLIC_MODE server

It plays both halves a person would: the MCP client (the SDK's own OAuth client: discovery, dynamic client
registration, PKCE, token exchange) and the browser (the connect page's wallet signature, then a plan's approval and
signing). A throwaway keypair stands in for the wallet; it only ever signs on the test fork.

What it proves, in order: discovery; the login with a wallet signature; the tool list; that reads are scoped to the
logged-in wallet; the faucet; one signed write (Earn supply) relayed and confirmed; the 1.15 health-factor floor and
the per-action limit refusing an unsafe borrow; a refused write for somebody else's wallet; a lever plan and the unwind
that undoes it. It exits non-zero on the first failed check.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770").rstrip("/")
wallet_kp = Keypair()
WALLET = str(wallet_kp.pubkey())
failures: list[str] = []


def check(label: str, ok: bool, detail: Any = "") -> None:
    print(("  PASS " if ok else "  FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(label)


class Storage:
    tokens: OAuthToken | None = None
    client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info


def sign_tx(unsigned_b64: str) -> str:
    tx = VersionedTransaction.from_bytes(base64.b64decode(unsigned_b64))
    return base64.b64encode(bytes(VersionedTransaction(tx.message, [wallet_kp]))).decode()


async def browser_login(authorization_url: str, outcome: dict[str, Any]) -> None:
    """What the connect page does: follow /authorize, sign the SIWS message with the wallet, follow the redirect."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as browser:
        first = await browser.get(authorization_url)
        consent = urlparse(first.headers["location"])
        req = parse_qs(consent.query)["req"][0]
        origin = f"{consent.scheme}://{consent.netloc}"
        headers = {"Origin": origin}
        outcome["shown"] = (await browser.get(f"{origin}/oauth/consent/request", params={"req": req}, headers=headers)).json()
        ch = (await browser.post(f"{origin}/oauth/consent/challenge", json={"req": req, "wallet": WALLET}, headers=headers)).json()
        sig = base64.b64encode(bytes(wallet_kp.sign_message(ch["message"].encode()))).decode()
        done = await browser.post(
            f"{origin}/oauth/consent/complete",
            json={"req": req, "wallet": WALLET, "nonce": ch["nonce"], "signature": sig}, headers=headers,
        )
        assert done.status_code == 200, done.text
        q = parse_qs(urlparse(done.json()["redirect_url"]).query)
        outcome["code"] = AuthorizationCodeResult(code=q["code"][0], state=q.get("state", [None])[0], iss=q.get("iss", [None])[0])


async def call(session: ClientSession, name: str, **args: Any) -> tuple[dict[str, Any], bool]:
    for attempt in range(5):
        try:
            res = await session.call_tool(name, args)
            break
        except Exception as exc:  # noqa: BLE001 - the per-caller rate limit answers 429 once the burst is spent
            if attempt == 4:
                raise
            print(f"   ({name}: {type(exc).__name__}; waiting for the rate limit to refill)")
            await asyncio.sleep(4)
    text = res.content[0].text  # type: ignore[union-attr]
    try:
        return json.loads(text), bool(res.is_error)
    except json.JSONDecodeError:
        return {"raw": text}, bool(res.is_error)


async def browser_run(plan_id: str, label: str) -> bool:
    """What plan.js does: approve with the wallet, then build, sign and submit each step until the plan is done."""
    api = f"{BASE}/api/plan/{plan_id}"
    async with httpx.AsyncClient(timeout=120) as http:
        view = (await http.get(api)).json()
        sig = base64.b64encode(bytes(wallet_kp.sign_message(view["approval_message"].encode()))).decode()
        r = await http.post(f"{api}/approve", json={"wallet": WALLET, "signature": sig})
        if r.status_code != 200:
            print("   approve failed", r.text[:200])
            return False
        for _ in range(40):
            n = (await http.post(f"{api}/next")).json()
            if n.get("done"):
                return True
            if n.get("skipped"):
                continue
            if n.get("waiting"):
                await asyncio.sleep(2)
                continue
            if n.get("ok") is False:
                print(f"   [{label}] step refused:", n.get("code"), n.get("reason"))
                return False
            for tx in n["transactions"]:
                s = await http.post(f"{api}/submit", json={"tx_id": tx["id"], "signed": sign_tx(tx["unsignedTransaction"])})
                if s.status_code != 200:
                    print(f"   [{label}] submit failed", s.status_code, s.text[:200])
                    return False
            state = ""
            for _ in range(45):
                v = (await http.get(api)).json()
                state = v["steps"][n["step"]]["state"]
                if state in ("confirmed", "failed", "expired", "uncertain"):
                    break
                await asyncio.sleep(2)
            print(f"   [{label}] step {n['step'] + 1} {n['label']}: {state}")
            if state != "confirmed":
                return False
    return False


async def main() -> int:
    print(f"connector: {BASE}\nwallet:    {WALLET}")
    async with httpx.AsyncClient(timeout=30) as http:
        prm = await http.get(f"{BASE}/.well-known/oauth-protected-resource/mcp")
        asm = await http.get(f"{BASE}/.well-known/oauth-authorization-server")
        jwks = await http.get(f"{BASE}/.well-known/jwks.json")
        check("discovery: protected-resource metadata", prm.status_code == 200, prm.status_code)
        check("discovery: authorization-server metadata", asm.status_code == 200 and asm.json().get("code_challenge_methods_supported") == ["S256"], asm.text[:120])
        check("discovery: JWKS published", jwks.status_code == 200 and jwks.json().get("keys"), jwks.status_code)
        anon = await http.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check("an anonymous call to /mcp is refused", anon.status_code == 401, anon.status_code)
        health = await http.get(f"{BASE}/healthz")
        check("health endpoint", health.status_code == 200, health.status_code)

    outcome: dict[str, Any] = {}

    async def redirect_handler(url: str) -> None:
        await browser_login(url, outcome)

    async def callback_handler() -> AuthorizationCodeResult:
        return outcome["code"]

    storage = Storage()
    provider = OAuthClientProvider(
        server_url=f"{BASE}/mcp",
        client_metadata=OAuthClientMetadata(
            client_name="Vanna smoke test", redirect_uris=["http://127.0.0.1:9/callback"],  # type: ignore[list-item]
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            scope="vanna:read vanna:write", token_endpoint_auth_method="none",
        ),
        storage=storage, redirect_handler=redirect_handler, callback_handler=callback_handler,
    )

    async with httpx2.AsyncClient(auth=provider, follow_redirects=True, timeout=120) as http2:
        async with streamable_http_client(f"{BASE}/mcp", http_client=http2) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                check("login with a wallet signature (OAuth + PKCE + DCR)", storage.tokens is not None and bool(storage.tokens.refresh_token))
                check("consent page showed the client and redirect host", outcome.get("shown", {}).get("redirect_host") == "127.0.0.1:9", outcome.get("shown"))
                tools = {t.name for t in (await session.list_tools()).tools}
                need = {"vanna_plan", "vanna_risk", "vanna_portfolio", "vanna_margin_trade", "vanna_earn_write", "vanna_wallet"}
                check("tool list includes the planner, risk and portfolio tools", need <= tools, sorted(need - tools))
                check("dev-only fork tools are not exposed", "vanna_fork" not in tools)

                pol, _ = await call(session, "vanna_risk", action="policy")
                check("safety layer active, 1.15 floor", pol.get("safety_layer_active") is True and pol.get("health_factor_floor") == 1.15, pol)

                summary, err = await call(session, "vanna_wallet", action="summary")
                check("reads default to the logged-in wallet", not err and WALLET in json.dumps(summary), str(summary)[:200])

                # Chain data is public, so a read may name any wallet; a WRITE may only ever act as the logged-in one.
                stranger = str(Keypair().pubkey())
                other, _ = await call(session, "vanna_wallet", action="summary", authority=stranger)
                check("a read may name another wallet (public chain data) and returns that wallet's data", other.get("authority") == stranger, other)
                bad_write, _ = await call(session, "vanna_earn_write", action="supply", owner=stranger, asset="USDC", amount="1")
                check("a write for another wallet is refused", bad_write.get("code") == "wallet_mismatch", bad_write)
                bad_plan, _ = await call(session, "vanna_plan", action="lever", authority=stranger, asset="SOL", leverage=2, borrow_asset="USDC", amount="1")
                check("a plan for another wallet is refused", bad_plan.get("code") == "wallet_mismatch" or bad_plan.get("ok") is False, bad_plan)

                fa, err = await call(session, "vanna_wallet", action="faucet")
                check("faucet funds the wallet", not err and not fa.get("error"), fa)

                sup, err = await call(session, "vanna_earn_write", action="supply", asset="USDC", amount="100")
                check("Earn supply built, simulated, with a signing link", not err and sup.get("simulation_success") is True and bool(sup.get("sign_url")), str(sup)[:300])
                if sup.get("plan_id"):
                    check("Earn supply signed in the wallet, relayed and confirmed", await browser_run(sup["plan_id"], "supply"))
                    st, _ = await call(session, "vanna_plan", action="status", plan_id=sup["plan_id"])
                    check("the plan reports complete", st.get("status") in ("complete", "completed", "done"), st.get("status"))

                big, _ = await call(session, "vanna_margin_trade", action="borrow", asset="USDC", amount="50000")
                check("an unsafe / oversized borrow is refused before a transaction is built", bool(big.get("error")) and "schemaVersion" not in big, str(big)[:200])

                pl, _ = await call(session, "vanna_plan", action="lever", asset="SOL", leverage=2, borrow_asset="USDC", amount="5")
                if pl.get("ok") and pl.get("executable"):
                    check("lever plan signed and executed", await browser_run(pl["plan_id"], "lever"))
                    un, _ = await call(session, "vanna_plan", action="unwind", scope="all")
                    if un.get("ok") and un.get("executable"):
                        check("unwind plan signed and executed", await browser_run(un["plan_id"], "unwind"))
                    else:
                        check("unwind plan drawn", False, str(un)[:200])
                else:
                    check("lever plan drawn", False, str(pl)[:300])

                again = await session.call_tool("vanna_risk", {"action": "policy"})
                check("the session still works after the plans", not again.is_error)

    # a refresh token rotates and the old one dies
    if storage.tokens and storage.tokens.refresh_token and storage.client_info:
        async with httpx.AsyncClient(timeout=30) as http:
            data = {"grant_type": "refresh_token", "refresh_token": storage.tokens.refresh_token, "client_id": storage.client_info.client_id}
            first = await http.post(f"{BASE}/token", data=data)
            replay = await http.post(f"{BASE}/token", data=data)
            check("refresh token rotates", first.status_code == 200 and first.json().get("refresh_token") != storage.tokens.refresh_token, first.text[:120])
            check("a replayed refresh token is refused", replay.status_code == 400, replay.status_code)
            rotated = first.json().get("refresh_token") if first.status_code == 200 else None
            if rotated:
                after = await http.post(f"{BASE}/token", data={**data, "refresh_token": rotated})
                check("replaying an old refresh token revokes the whole family", after.status_code == 400, after.status_code)

    print(f"\n{'ALL CHECKS PASSED' if not failures else str(len(failures)) + ' CHECK(S) FAILED: ' + '; '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
