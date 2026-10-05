"""Offline tests for the public connector's wallet-as-login OAuth server (mcp_server/oauth/).

Drives the real SDK handlers and the real streamable-HTTP MCP app in-process: dynamic client registration,
/authorize, the SIWS consent step (with a real ed25519 wallet), /token, refresh rotation, revocation, and a
tools/call that proves the wallet is enforced. No RPC, no network."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from solders.keypair import Keypair
from starlette.testclient import TestClient

import mcp_server.main as main
from mcp.shared.auth import OAuthClientInformationFull
from mcp_server.auth import AuthError
from mcp_server.oauth.keys import KeySet
from mcp_server.oauth.provider import OAuthConfig, VannaOAuthProvider, sanitize_client_name, validate_redirect_uri
from mcp_server.oauth.store import MemoryRecordStore
from mcp_server.oauth.verifier import LocalTokenVerifier
from vanna_core.config import VannaSettings

ISSUER = "https://mcp.test"
CONSENT_URL = "https://app.test/mcp/connect"
CONSENT_ORIGIN = "https://app.test"
RESOURCE = f"{ISSUER}/mcp"
REDIRECT = "http://127.0.0.1:8123/callback"


class Clock:
    """Wall-clock plus an offset, so a test can age pending records without sleeping."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


def _settings(**overrides: Any) -> VannaSettings:
    base: dict[str, Any] = dict(
        public_mode=True,
        auth_enabled=True,
        external_base_url=ISSUER,
        oauth_consent_url=CONSENT_URL,
        oauth_allow_ephemeral_key=True,
        rate_limit_enabled=False,
    )
    base.update(overrides)
    return VannaSettings(**base)


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> VannaSettings:
    s = _settings(**overrides)
    monkeypatch.setattr(main, "settings", s)
    monkeypatch.setattr("vanna_core.config.get_settings", lambda: s)
    return s


# ---------------------------------------------------------------- stub MCP server (records what the tools received)


def _stub_server(calls: list[dict[str, Any]]):
    server = main.ScopedMCPServer(name="connector-test")

    async def vanna_earn_write(action: str, owner: str, asset: str = "USDC", amount: str = "1") -> dict[str, Any]:
        calls.append({"tool": "vanna_earn_write", "action": action, "owner": owner})
        return {"built": action, "owner": owner}

    async def vanna_margin_status(action: str, authority: str) -> dict[str, Any]:
        calls.append({"tool": "vanna_margin_status", "action": action, "authority": authority})
        return {"read": action, "authority": authority}

    async def vanna_margin_trade(
        action: str, authority: str, asset: str = "USDC", amount: str = "1", payer: str | None = None
    ) -> dict[str, Any]:
        calls.append({"tool": "vanna_margin_trade", "action": action, "authority": authority, "payer": payer})
        return {"built": action, "authority": authority, "payer": payer}

    for fn in (vanna_earn_write, vanna_margin_status, vanna_margin_trade):
        server.add_tool(fn)
    return server


class Connector:
    """A started public connector plus helpers that act as an MCP client and as the consent page."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
        self.settings = _patch_settings(monkeypatch, **overrides)
        self.clock = Clock()
        self.store = MemoryRecordStore(clock=self.clock)
        self.calls: list[dict[str, Any]] = []
        self.app = main.build_public_app(store=self.store, server=_stub_server(self.calls))
        self.client = TestClient(self.app, follow_redirects=False)
        self.wallet = Keypair()

    def __enter__(self) -> "Connector":
        self.client.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.client.__exit__(*exc)

    # -- protocol steps

    @staticmethod
    def pkce() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return verifier, challenge

    def register(self, **meta: Any) -> dict[str, Any]:
        body = {
            "client_name": "Test Client",
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            **meta,
        }
        r = self.client.post("/register", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def authorize(self, client_id: str, challenge: str, **extra: Any) -> str:
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st-1",
            **extra,
        }
        r = self.client.get("/authorize", params=params)
        assert r.status_code == 302, r.text
        location = r.headers["location"]
        assert location.startswith(CONSENT_URL + "?req="), location
        return parse_qs(urlparse(location).query)["req"][0]

    def consent_headers(self) -> dict[str, str]:
        return {"Origin": CONSENT_ORIGIN}

    def challenge(self, req: str, wallet: Keypair | None = None, scopes: list[str] | None = None):
        wallet = wallet or self.wallet
        body: dict[str, Any] = {"req": req, "wallet": str(wallet.pubkey())}
        if scopes is not None:
            body["scopes"] = scopes
        return self.client.post("/oauth/consent/challenge", json=body, headers=self.consent_headers())

    @staticmethod
    def sign(wallet: Keypair, message: str) -> str:
        return base64.b64encode(bytes(wallet.sign_message(message.encode()))).decode()

    def complete(self, req: str, nonce: str, signature: str, wallet: Keypair | None = None, headers=None):
        wallet = wallet or self.wallet
        return self.client.post(
            "/oauth/consent/complete",
            json={"req": req, "wallet": str(wallet.pubkey()), "nonce": nonce, "signature": signature},
            headers=headers if headers is not None else self.consent_headers(),
        )

    def login(self, scopes: list[str] | None = None, wallet: Keypair | None = None) -> dict[str, Any]:
        """Register, authorize, sign in with the wallet, redeem the code. Returns tokens + context."""
        wallet = wallet or self.wallet
        client = self.register()
        verifier, challenge_ = self.pkce()
        req = self.authorize(client["client_id"], challenge_)
        ch = self.challenge(req, wallet, scopes)
        assert ch.status_code == 200, ch.text
        data = ch.json()
        done = self.complete(req, data["nonce"], self.sign(wallet, data["message"]), wallet)
        assert done.status_code == 200, done.text
        redirect = urlparse(done.json()["redirect_url"])
        query = parse_qs(redirect.query)
        assert query["state"] == ["st-1"] and query["iss"] == [ISSUER]
        token = self.token_request(client["client_id"], code=query["code"][0], verifier=verifier)
        assert token.status_code == 200, token.text
        return {"client": client, "verifier": verifier, "tokens": token.json(), "code": query["code"][0], "req": req}

    def token_request(self, client_id: str, *, code: str, verifier: str, redirect_uri: str = REDIRECT):
        return self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
        )

    def refresh(self, client_id: str, refresh_token: str):
        return self.client.post(
            "/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token}
        )

    # -- MCP

    def mcp(self, access_token: str):
        return _McpSession(self.client, access_token)


class _McpSession:
    def __init__(self, client: TestClient, token: str) -> None:
        self._client = client
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._id = 0
        init = self.rpc(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        )
        assert "result" in init, init
        self._headers["mcp-protocol-version"] = init["result"]["protocolVersion"]

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        r = self._client.post(
            "/mcp", headers=self._headers, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        )
        assert r.status_code == 200, (r.status_code, r.text)
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            data = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
            return json.loads(data[-1])
        return r.json()

    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})["result"]
        return json.loads(result["content"][0]["text"])


@pytest.fixture
def connector(monkeypatch: pytest.MonkeyPatch):
    with Connector(monkeypatch) as c:
        yield c


# ================================================================ discovery


def test_metadata_documents_describe_our_own_authorization_server(connector: Connector) -> None:
    as_meta = connector.client.get("/.well-known/oauth-authorization-server").json()
    assert as_meta["issuer"] == ISSUER
    assert as_meta["authorization_endpoint"] == f"{ISSUER}/authorize"
    assert as_meta["registration_endpoint"] == f"{ISSUER}/register"
    assert as_meta["code_challenge_methods_supported"] == ["S256"]
    assert "none" in as_meta["token_endpoint_auth_methods_supported"]
    # The path-aware form some clients probe.
    assert connector.client.get("/.well-known/oauth-authorization-server/mcp").json()["issuer"] == ISSUER

    resource = connector.client.get("/.well-known/oauth-protected-resource").json()
    assert resource["resource"] == RESOURCE
    assert resource["authorization_servers"] == [ISSUER]
    assert set(resource["scopes_supported"]) == {"vanna:read", "vanna:write"}

    jwks = connector.client.get("/.well-known/jwks.json").json()
    assert [k["alg"] for k in jwks["keys"]] == ["ES256"]
    assert "d" not in jwks["keys"][0]  # the private scalar is never published


def test_mcp_requires_a_token_and_points_at_metadata(connector: Connector) -> None:
    r = connector.client.post("/mcp", json={})
    assert r.status_code == 401
    assert 'resource_metadata="https://mcp.test/.well-known/oauth-protected-resource"' in r.headers["www-authenticate"]


# ================================================================ dynamic client registration


def test_registration_accepts_https_loopback_and_private_scheme_redirects(connector: Connector) -> None:
    for uri in ("https://client.example/cb", "http://localhost:9999/cb", "http://127.0.0.1/cb", "cursor://anysphere.cursor/cb"):
        assert connector.register(redirect_uris=[uri])["client_id"]


@pytest.mark.parametrize(
    "uri",
    [
        "http://evil.example/cb",  # plain http off loopback
        "javascript:alert(1)",
        "data:text/html,x",
        "file:///etc/passwd",
        "https://client.example/cb#frag",
        "https://*.example.com/cb",
        "https://user:pw@client.example/cb",
    ],
)
def test_registration_rejects_dangerous_redirects(connector: Connector, uri: str) -> None:
    r = connector.client.post("/register", json={"redirect_uris": [uri], "token_endpoint_auth_method": "none"})
    assert r.status_code == 400, (uri, r.text)
    assert r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")


def test_registration_limits_redirects_scopes_and_grants(connector: Connector) -> None:
    many = [f"https://c{i}.example/cb" for i in range(6)]
    assert connector.client.post("/register", json={"redirect_uris": many}).status_code == 400
    assert connector.client.post(
        "/register", json={"redirect_uris": [REDIRECT], "scope": "vanna:read admin"}
    ).status_code == 400
    assert connector.client.post(
        "/register",
        json={"redirect_uris": [REDIRECT], "grant_types": ["authorization_code", "client_credentials"]},
    ).status_code == 400


def test_client_name_is_sanitized() -> None:
    assert sanitize_client_name('Evil"\n\nSign me\x00') == "Evil' Sign me"
    assert sanitize_client_name("   ") == "Unnamed MCP client"
    assert len(sanitize_client_name("x" * 500)) == 80
    assert validate_redirect_uri("https://ok.example/cb") is None
    assert validate_redirect_uri("vscode://x/cb") is None
    assert validate_redirect_uri("http://example.com/cb") is not None


def test_registration_is_rate_limited_per_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    with Connector(monkeypatch, oauth_dcr_per_ip_per_hour=3) as c:
        for _ in range(3):
            c.register()
        r = c.client.post("/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"})
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1


def test_registration_has_a_global_daily_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    with Connector(monkeypatch, oauth_dcr_global_per_day=2) as c:
        c.register()
        c.register()
        r = c.client.post("/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"})
        assert r.status_code == 400


# ================================================================ authorize


def test_authorize_rejects_unregistered_redirect_and_unknown_client(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    params = {
        "client_id": client["client_id"],
        "response_type": "code",
        "redirect_uri": "http://127.0.0.1:9/other",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    assert connector.client.get("/authorize", params=params).status_code == 400
    params.update(client_id="nope", redirect_uri=REDIRECT)
    assert connector.client.get("/authorize", params=params).status_code == 400


def test_authorize_requires_pkce_s256(connector: Connector) -> None:
    client = connector.register()
    params = {"client_id": client["client_id"], "response_type": "code", "redirect_uri": REDIRECT}
    assert connector.client.get("/authorize", params=params).status_code in (302, 400)
    r = connector.client.get("/authorize", params=params)
    assert "consent" not in r.headers.get("location", "")  # no PKCE challenge -> never reaches the consent page


def test_authorize_binds_the_resource_to_this_server(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    base = {
        "client_id": client["client_id"],
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    ok = connector.client.get("/authorize", params={**base, "resource": RESOURCE})
    assert ok.status_code == 302 and ok.headers["location"].startswith(CONSENT_URL)
    bad = connector.client.get("/authorize", params={**base, "resource": "https://other.example/mcp"})
    assert bad.status_code == 302
    assert "error=invalid_target" in bad.headers["location"] and bad.headers["location"].startswith(REDIRECT)


# ================================================================ consent page API


def test_consent_describes_the_request_without_secrets(connector: Connector) -> None:
    client = connector.register(client_name="My Agent")
    verifier, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    r = connector.client.get(f"/oauth/consent/request?req={req}", headers=connector.consent_headers())
    assert r.status_code == 200
    data = r.json()
    assert data["client_name"] == "My Agent"
    assert data["redirect_host"] == "127.0.0.1:8123"
    assert data["requested_scopes"] == ["vanna:read", "vanna:write"]
    assert data["client_verified"] is False and data["domain"] == "app.test"
    assert challenge not in r.text and "st-1" not in r.text
    assert r.headers["access-control-allow-origin"] == CONSENT_ORIGIN
    assert r.headers["cache-control"] == "no-store"


def test_consent_rejects_foreign_or_missing_origin_on_posts(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    body = {"req": req, "wallet": str(connector.wallet.pubkey())}
    assert connector.client.post("/oauth/consent/challenge", json=body).status_code == 403
    assert connector.client.post(
        "/oauth/consent/challenge", json=body, headers={"Origin": "https://evil.example"}
    ).status_code == 403
    assert connector.client.get(
        f"/oauth/consent/request?req={req}", headers={"Origin": "https://evil.example"}
    ).status_code == 403
    preflight = connector.client.options(
        "/oauth/consent/challenge", headers={"Origin": CONSENT_ORIGIN, "Access-Control-Request-Method": "POST"}
    )
    assert preflight.status_code == 204 and preflight.headers["access-control-allow-origin"] == CONSENT_ORIGIN


def test_consent_unknown_or_malformed_request_ids(connector: Connector) -> None:
    for req in ("x", "a" * 43, ""):
        r = connector.client.get(f"/oauth/consent/request?req={req}", headers=connector.consent_headers())
        assert r.status_code in (404, 410)
        assert "error" in r.json()


def test_siws_message_binds_everything_the_wallet_authorizes(connector: Connector) -> None:
    client = connector.register(client_name='Agent "X"')
    _, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    message = connector.challenge(req).json()["message"]
    wallet = str(connector.wallet.pubkey())
    assert message.startswith(f"app.test wants you to sign in with your Solana account:\n{wallet}\n")
    for needle in (
        "Agent 'X'",
        "127.0.0.1:8123",
        f"URI: {ISSUER}",
        f"Client ID: {client['client_id']}",
        "Scopes: vanna:read vanna:write",
        f"Request ID: {req}",
        "Nonce: ",
        "Expiration Time: ",
        "does not move funds",
    ):
        assert needle in message, needle


def test_challenge_rejects_bad_wallet_and_scope_widening(connector: Connector) -> None:
    client = connector.register(scope="vanna:read")
    _, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    bad = connector.client.post(
        "/oauth/consent/challenge", json={"req": req, "wallet": "not-a-wallet"}, headers=connector.consent_headers()
    )
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_wallet"
    wide = connector.challenge(req, scopes=["vanna:read", "vanna:write"])
    assert wide.status_code == 400 and wide.json()["error"] == "invalid_scope"
    empty = connector.challenge(req, scopes=[])
    assert empty.status_code == 400


def test_challenges_per_request_are_capped(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    statuses = [connector.challenge(req).status_code for _ in range(7)]
    assert statuses[:5] == [200] * 5 and statuses[5:] == [429, 429]


# ================================================================ the login: SIWS proof


def test_full_login_issues_a_wallet_token(connector: Connector) -> None:
    session = connector.login()
    tokens = session["tokens"]
    assert tokens["token_type"] == "Bearer" and tokens["scope"] == "vanna:read vanna:write"
    assert tokens["refresh_token"].startswith("vrt_")
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    wallet = str(connector.wallet.pubkey())
    assert claims["sub"] == wallet and claims["vanna_wallet"] == wallet
    assert claims["iss"] == ISSUER and claims["aud"] == RESOURCE
    assert claims["client_id"] == session["client"]["client_id"]
    assert claims["exp"] - claims["iat"] == 900
    header = jwt.get_unverified_header(tokens["access_token"])
    assert header["alg"] == "ES256" and header["typ"] == "at+jwt"


def test_login_with_a_signature_from_another_key_is_refused(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    data = connector.challenge(req).json()
    attacker = Keypair()
    r = connector.complete(req, data["nonce"], connector.sign(attacker, data["message"]))
    assert r.status_code == 401 and r.json()["error"] == "invalid_signature"
    # The pending authorization survives a bad signature, so the real owner can still finish...
    again = connector.challenge(req).json()
    ok = connector.complete(req, again["nonce"], connector.sign(connector.wallet, again["message"]))
    assert ok.status_code == 200


def test_signature_over_a_different_message_is_refused(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    data = connector.challenge(req).json()
    tampered = data["message"].replace("vanna:read vanna:write", "vanna:read")
    r = connector.complete(req, data["nonce"], connector.sign(connector.wallet, tampered))
    assert r.status_code == 401


def test_nonce_cannot_be_replayed(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    data = connector.challenge(req).json()
    signature = connector.sign(connector.wallet, data["message"])
    assert connector.complete(req, data["nonce"], signature).status_code == 200
    replay = connector.complete(req, data["nonce"], signature)
    assert replay.status_code == 400 and replay.json()["error"] == "challenge_invalid"


def test_challenge_for_one_wallet_cannot_complete_for_another(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    data = connector.challenge(req).json()
    other = Keypair()
    r = connector.complete(req, data["nonce"], connector.sign(other, data["message"]), wallet=other)
    assert r.status_code == 400 and r.json()["error"] == "challenge_invalid"


def test_challenge_for_one_request_cannot_complete_another(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    req_a = connector.authorize(client["client_id"], challenge)
    req_b = connector.authorize(client["client_id"], challenge)
    data_a = connector.challenge(req_a).json()
    r = connector.complete(req_b, data_a["nonce"], connector.sign(connector.wallet, data_a["message"]))
    assert r.status_code == 400 and r.json()["error"] == "challenge_invalid"


def test_pending_authorization_is_single_use_and_expires(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    data = connector.challenge(req).json()
    signature = connector.sign(connector.wallet, data["message"])
    assert connector.complete(req, data["nonce"], signature).status_code == 200
    # Already used: a fresh challenge for the same request id is gone.
    assert connector.challenge(req).status_code == 410

    req2 = connector.authorize(client["client_id"], challenge)
    data2 = connector.challenge(req2).json()
    connector.clock.offset += 601  # past the 10-minute pending lifetime (and the 2-minute SIWS expiry)
    r = connector.complete(req2, data2["nonce"], connector.sign(connector.wallet, data2["message"]))
    assert r.status_code == 400  # the challenge itself expired first
    assert connector.client.get(f"/oauth/consent/request?req={req2}").status_code == 410


def test_denial_returns_access_denied_to_the_client(connector: Connector) -> None:
    client = connector.register()
    _, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    r = connector.client.post("/oauth/consent/deny", json={"req": req}, headers=connector.consent_headers())
    assert r.status_code == 200
    redirect = r.json()["redirect_url"]
    assert redirect.startswith(REDIRECT) and "error=access_denied" in redirect and "state=st-1" in redirect
    assert connector.client.post(
        "/oauth/consent/deny", json={"req": req}, headers=connector.consent_headers()
    ).status_code == 410


def test_user_can_narrow_to_read_only(connector: Connector) -> None:
    session = connector.login(scopes=["vanna:read"])
    assert session["tokens"]["scope"] == "vanna:read"
    mcp = connector.mcp(session["tokens"]["access_token"])
    assert mcp.tool_names() == {"vanna_margin_status"}  # write-only tools are hidden without write scope
    refused = mcp.call("vanna_earn_write", action="supply", owner=str(connector.wallet.pubkey()))
    assert refused["code"] == "insufficient_scope"


# ================================================================ token endpoint


def test_token_requires_the_pkce_verifier(connector: Connector) -> None:
    client = connector.register()
    verifier, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    data = connector.challenge(req).json()
    done = connector.complete(req, data["nonce"], connector.sign(connector.wallet, data["message"]))
    code = parse_qs(urlparse(done.json()["redirect_url"]).query)["code"][0]
    wrong = connector.token_request(client["client_id"], code=code, verifier="wrong-" + verifier)
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    assert connector.token_request(client["client_id"], code=code, verifier=verifier).status_code == 200


def test_code_is_bound_to_its_redirect_and_client(connector: Connector) -> None:
    client = connector.register(redirect_uris=[REDIRECT, "http://127.0.0.1:8123/other"])
    other = connector.register()
    verifier, challenge = connector.pkce()
    req = connector.authorize(client["client_id"], challenge)
    data = connector.challenge(req).json()
    done = connector.complete(req, data["nonce"], connector.sign(connector.wallet, data["message"]))
    code = parse_qs(urlparse(done.json()["redirect_url"]).query)["code"][0]
    assert connector.token_request(
        client["client_id"], code=code, verifier=verifier, redirect_uri="http://127.0.0.1:8123/other"
    ).status_code == 400
    assert connector.token_request(other["client_id"], code=code, verifier=verifier).status_code == 400
    assert connector.token_request(client["client_id"], code=code, verifier=verifier).status_code == 200


def test_replayed_code_revokes_what_it_produced(connector: Connector) -> None:
    session = connector.login()
    access = session["tokens"]["access_token"]
    assert connector.mcp(access).tool_names()  # works now
    replay = connector.token_request(session["client"]["client_id"], code=session["code"], verifier=session["verifier"])
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    # ... and the first redeemer's tokens are now dead (RFC 6749 §4.1.2).
    r = connector.client.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json={})
    assert r.status_code == 401 and "revoked" in r.json()["detail"]
    assert connector.refresh(session["client"]["client_id"], session["tokens"]["refresh_token"]).status_code == 400


def test_refresh_rotates_and_reuse_kills_the_family(connector: Connector) -> None:
    session = connector.login()
    cid = session["client"]["client_id"]
    first_refresh = session["tokens"]["refresh_token"]
    rotated = connector.refresh(cid, first_refresh)
    assert rotated.status_code == 200
    body = rotated.json()
    assert body["refresh_token"] != first_refresh and body["access_token"] != session["tokens"]["access_token"]
    assert connector.mcp(body["access_token"]).tool_names()

    # The old refresh token is single use: presenting it again is theft.
    reuse = connector.refresh(cid, first_refresh)
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    # ... and it burned the legitimate descendant too.
    assert connector.refresh(cid, body["refresh_token"]).status_code == 400
    assert connector.client.post(
        "/mcp", headers={"Authorization": f"Bearer {body['access_token']}"}, json={}
    ).status_code == 401


def test_refresh_token_belongs_to_one_client(connector: Connector) -> None:
    session = connector.login()
    stranger = connector.register()
    r = connector.refresh(stranger["client_id"], session["tokens"]["refresh_token"])
    assert r.status_code == 400
    assert connector.refresh(session["client"]["client_id"], session["tokens"]["refresh_token"]).status_code == 200


def test_refresh_cannot_widen_scope(connector: Connector) -> None:
    session = connector.login(scopes=["vanna:read"])
    r = connector.client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": session["client"]["client_id"],
            "refresh_token": session["tokens"]["refresh_token"],
            "scope": "vanna:read vanna:write",
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope"


def test_refresh_token_has_an_absolute_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    with Connector(monkeypatch, oauth_refresh_token_ttl_seconds=3600) as c:
        session = c.login()
        cid = session["client"]["client_id"]
        rotated = c.refresh(cid, session["tokens"]["refresh_token"]).json()
        c.clock.offset += 3700  # past the absolute expiry
        assert c.refresh(cid, rotated["refresh_token"]).status_code == 400


def test_revocation_endpoint_kills_the_grant(connector: Connector) -> None:
    session = connector.login()
    cid = session["client"]["client_id"]
    access = session["tokens"]["access_token"]
    assert connector.mcp(access).tool_names()
    r = connector.client.post(
        "/revoke", data={"token": session["tokens"]["refresh_token"], "client_id": cid, "token_type_hint": "refresh_token"}
    )
    assert r.status_code == 200
    assert connector.client.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json={}).status_code == 401
    assert connector.refresh(cid, session["tokens"]["refresh_token"]).status_code == 400


def test_revocation_by_another_client_does_nothing(connector: Connector) -> None:
    session = connector.login()
    stranger = connector.register()
    connector.client.post(
        "/revoke", data={"token": session["tokens"]["refresh_token"], "client_id": stranger["client_id"]}
    )
    assert connector.mcp(session["tokens"]["access_token"]).tool_names()


# ================================================================ token verification


@pytest.fixture
def verifier_parts():
    keys = KeySet.ephemeral()
    return keys, LocalTokenVerifier(keys, ISSUER, RESOURCE)


def _claims(wallet: str, **over: Any) -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": ISSUER,
        "aud": RESOURCE,
        "sub": wallet,
        "vanna_wallet": wallet,
        "client_id": "c1",
        "scope": "vanna:read",
        "jti": "j1",
        "iat": now,
        "exp": now + 300,
        **over,
    }


def test_verifier_accepts_a_well_formed_wallet_token(verifier_parts) -> None:
    keys, verifier = verifier_parts
    wallet = str(Keypair().pubkey())
    assert verifier.verify(keys.sign(_claims(wallet)))["vanna_wallet"] == wallet


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda c: {**c, "exp": int(time.time()) - 3600}, "expired"),
        (lambda c: {**c, "aud": "https://other.example/mcp"}, "audience"),
        (lambda c: {**c, "iss": "https://evil.example"}, "issuer"),
        (lambda c: {**c, "sub": str(Keypair().pubkey())}, "Invalid token"),  # sub != wallet claim
        (lambda c: {**c, "vanna_wallet": "not-a-wallet", "sub": "not-a-wallet"}, "Invalid token"),
        (lambda c: {k: v for k, v in c.items() if k != "client_id"}, "Invalid token"),
        (lambda c: {k: v for k, v in c.items() if k != "jti"}, "Invalid token"),
    ],
)
def test_verifier_rejects_malformed_claims(verifier_parts, mutate, message: str) -> None:
    keys, verifier = verifier_parts
    wallet = str(Keypair().pubkey())
    with pytest.raises(AuthError, match=message):
        verifier.verify(keys.sign(mutate(_claims(wallet))))


def test_verifier_rejects_foreign_keys_algorithms_and_types(verifier_parts) -> None:
    keys, verifier = verifier_parts
    wallet = str(Keypair().pubkey())
    foreign = ec.generate_private_key(ec.SECP256R1())
    forged = jwt.encode(_claims(wallet), foreign, algorithm="ES256", headers={"kid": keys.kid, "typ": "at+jwt"})
    with pytest.raises(AuthError):
        verifier.verify(forged)
    unknown_kid = jwt.encode(_claims(wallet), foreign, algorithm="ES256", headers={"kid": "nope", "typ": "at+jwt"})
    with pytest.raises(AuthError):
        verifier.verify(unknown_kid)
    wrong_typ = jwt.encode(_claims(wallet), keys._private, algorithm="ES256", headers={"kid": keys.kid, "typ": "JWT"})
    with pytest.raises(AuthError):
        verifier.verify(wrong_typ)
    none_alg = jwt.encode(_claims(wallet), None, algorithm="none", headers={"kid": keys.kid, "typ": "at+jwt"})
    with pytest.raises(AuthError):
        verifier.verify(none_alg)
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rs = jwt.encode(_claims(wallet), rsa_key, algorithm="RS256", headers={"kid": keys.kid, "typ": "at+jwt"})
    with pytest.raises(AuthError):
        verifier.verify(rs)
    with pytest.raises(AuthError):
        verifier.verify("not.a.jwt")


def test_a_workos_style_token_is_rejected_by_the_public_service(connector: Connector) -> None:
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    token = jwt.encode(
        {"iss": "https://api.workos.com", "aud": RESOURCE, "sub": "app-client", "azp": "app-client", "exp": now + 300},
        rsa_key,
        algorithm="RS256",
    )
    assert connector.client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={}).status_code == 401


def test_key_rotation_keeps_old_tokens_valid_until_expiry() -> None:
    old = KeySet.ephemeral()
    old_public_pem = old._private.public_key().public_bytes(
        __import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
        __import__("cryptography").hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    new_pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        __import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
        __import__("cryptography").hazmat.primitives.serialization.PrivateFormat.PKCS8,
        __import__("cryptography").hazmat.primitives.serialization.NoEncryption(),
    ).decode()
    rotated = KeySet.from_pem(new_pem, old_public_pem)
    assert len(rotated.jwks()["keys"]) == 2 and rotated.kid != old.kid
    wallet = str(Keypair().pubkey())
    assert LocalTokenVerifier(rotated, ISSUER, RESOURCE).verify(old.sign(_claims(wallet)))["sub"] == wallet


# ================================================================ the wallet is enforced on tool calls


def test_calls_default_to_and_are_forced_to_the_logged_in_wallet(connector: Connector) -> None:
    session = connector.login()
    mcp = connector.mcp(session["tokens"]["access_token"])
    me = str(connector.wallet.pubkey())

    # Omitted wallet argument -> the token's wallet (reads and writes).
    assert mcp.call("vanna_margin_status", action="health")["authority"] == me
    assert mcp.call("vanna_earn_write", action="supply")["owner"] == me
    assert connector.calls[-1] == {"tool": "vanna_earn_write", "action": "supply", "owner": me}
    # Explicitly naming yourself is fine.
    assert mcp.call("vanna_earn_write", action="supply", owner=me)["owner"] == me

    other = str(Keypair().pubkey())
    refused = mcp.call("vanna_earn_write", action="supply", owner=other)
    assert refused["error"] == "forbidden" and refused["code"] == "wallet_mismatch"
    assert connector.calls[-1]["owner"] == me or all(c["owner"] != other for c in connector.calls if "owner" in c)
    refused = mcp.call("vanna_margin_trade", action="borrow", authority=other)
    assert refused["code"] == "wallet_mismatch"
    # Reads of other public addresses stay allowed.
    assert mcp.call("vanna_margin_status", action="health", authority=other)["authority"] == other


def test_repay_from_wallet_makes_the_caller_the_payer(connector: Connector) -> None:
    session = connector.login()
    mcp = connector.mcp(session["tokens"]["access_token"])
    me, debtor = str(connector.wallet.pubkey()), str(Keypair().pubkey())
    built = mcp.call("vanna_margin_trade", action="repay_from_wallet", authority=debtor)
    assert built["payer"] == me and built["authority"] == debtor  # anyone may repay someone's debt
    assert mcp.call("vanna_margin_trade", action="repay_from_wallet", authority=debtor, payer=debtor)["code"] == "wallet_mismatch"


def test_two_logins_are_isolated(connector: Connector) -> None:
    alice = connector.login()
    bob_wallet = Keypair()
    bob = connector.login(wallet=bob_wallet)
    a = connector.mcp(alice["tokens"]["access_token"]).call("vanna_margin_status", action="health")
    b = connector.mcp(bob["tokens"]["access_token"]).call("vanna_margin_status", action="health")
    assert a["authority"] == str(connector.wallet.pubkey()) and b["authority"] == str(bob_wallet.pubkey())
    assert connector.mcp(bob["tokens"]["access_token"]).call(
        "vanna_earn_write", action="supply", owner=str(connector.wallet.pubkey())
    )["code"] == "wallet_mismatch"


# ================================================================ configuration safety


def _build(monkeypatch: pytest.MonkeyPatch, **overrides: Any):
    _patch_settings(monkeypatch, **overrides)
    return main.build_public_app(store=MemoryRecordStore(), server=_stub_server([]))


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"auth_enabled": False}, "AUTH_ENABLED"),
        ({"first_party_client_ids": "copilot"}, "FIRST_PARTY"),
        ({"enable_fork_tools": True}, "fork"),
        ({"external_base_url": ""}, "EXTERNAL_BASE_URL"),
        ({"external_base_url": "http://mcp.example.com"}, "https"),
        ({"oauth_consent_url": "http://app.example.com/connect"}, "https"),
        ({"oauth_allow_ephemeral_key": False}, "OAUTH_SIGNING_KEY_PEM"),
    ],
)
def test_public_mode_refuses_unsafe_configuration(monkeypatch: pytest.MonkeyPatch, overrides, match: str) -> None:
    with pytest.raises(RuntimeError, match=match):
        _build(monkeypatch, **overrides)


def test_consent_page_defaults_to_this_services_own_connect_page() -> None:
    config = OAuthConfig.from_settings(_settings(oauth_consent_url=""))
    assert config.consent_url == f"{ISSUER}/connect" and config.consent_origin == ISSUER


def test_loopback_http_is_allowed_for_local_development(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build(
        monkeypatch, external_base_url="http://localhost:8000", oauth_consent_url="http://localhost:3000/mcp/connect"
    )
    assert app is not None


def test_signing_key_must_be_es256() -> None:
    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        __import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
        __import__("cryptography").hazmat.primitives.serialization.PrivateFormat.PKCS8,
        __import__("cryptography").hazmat.primitives.serialization.NoEncryption(),
    ).decode()
    with pytest.raises(ValueError, match="ES256"):
        KeySet.from_pem(rsa_pem)


def test_private_service_is_unchanged_by_the_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    """With PUBLIC_MODE off, no wallet identity is ever honoured, even from a token that claims one."""
    from mcp_server import identity

    s = _settings(public_mode=False)
    monkeypatch.setattr("vanna_core.config.get_settings", lambda: s)
    tokens = identity.set_identity({"iss": ISSUER, "sub": "w", "vanna_wallet": "w"}, None)
    try:
        assert identity.caller_wallet() is None
    finally:
        identity.reset_identity(tokens)


def test_wallet_claim_needs_this_services_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_server import identity

    wallet = str(Keypair().pubkey())
    s = _settings()
    monkeypatch.setattr("vanna_core.config.get_settings", lambda: s)
    for iss, expected in ((ISSUER, wallet), ("https://api.workos.com", None)):
        tokens = identity.set_identity({"iss": iss, "sub": wallet, "vanna_wallet": wallet}, None)
        try:
            assert identity.caller_wallet() == expected
        finally:
            identity.reset_identity(tokens)
    tokens = identity.set_identity({"iss": ISSUER, "sub": "someone-else", "vanna_wallet": wallet}, None)
    try:
        assert identity.caller_wallet() is None  # sub and wallet claim must agree
    finally:
        identity.reset_identity(tokens)


async def test_concurrent_redemption_of_one_code_has_one_winner_and_revokes_the_grant() -> None:
    import asyncio

    from mcp.server.auth.provider import TokenError

    provider = VannaOAuthProvider(MemoryRecordStore(), KeySet.ephemeral(), OAuthConfig(issuer=ISSUER, consent_url=CONSENT_URL))
    client = OAuthClientInformationFull(client_id="c1", redirect_uris=[REDIRECT], token_endpoint_auth_method="none")  # type: ignore[list-item]
    pending = {"code_challenge": "x", "redirect_uri": REDIRECT, "redirect_uri_provided_explicitly": True}
    wallet = str(Keypair().pubkey())
    code = await provider.issue_authorization_code(client_id="c1", wallet=wallet, scopes=["vanna:read"], pending=pending)
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    results = await asyncio.gather(
        provider.exchange_authorization_code(client, loaded),
        provider.exchange_authorization_code(client, loaded),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, Exception)]
    losers = [r for r in results if isinstance(r, TokenError)]
    assert len(winners) == 1 and len(losers) == 1
    assert not await provider.is_token_active(jwt.decode(winners[0].access_token, options={"verify_signature": False}))


def test_provider_is_independent_of_the_transport() -> None:
    """The provider can be constructed and used without any HTTP app (store + keys + config only)."""
    config = OAuthConfig(issuer=ISSUER, consent_url=CONSENT_URL)
    provider = VannaOAuthProvider(MemoryRecordStore(), KeySet.ephemeral(), config)
    assert config.resource == RESOURCE and config.consent_origin == CONSENT_ORIGIN and config.consent_domain == "app.test"
    assert provider.keys.jwks()["keys"]
