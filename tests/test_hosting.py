"""Offline tests for the hosting layer: scopes, identity, auth, rate limit,
and the scoped MCP server over real streamable HTTP. No RPC is touched."""

from __future__ import annotations

import json
import time
import typing
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from mcp_server import identity
from mcp_server.auth import AuthError, AuthMiddleware, TokenVerifier, is_public_path
from mcp_server.dispatchers import ALL_DISPATCHERS, FORK_DISPATCHERS
from mcp_server.rate_limit import (
    CircuitBreaker,
    RateLimitMiddleware,
    TokenBucket,
    caller_key,
    rpc_tool_call,
    tool_cost,
)
from mcp_server.scopes import WRITE_ACTIONS, WRITE_ONLY_TOOLS, is_write_call
from vanna_core.config import VannaSettings

ISSUER = "https://idp.example.com"
AUDIENCE = "https://mcp.example.com"


def _actions(fn: Any) -> set[str]:
    return set(typing.get_args(typing.get_type_hints(fn)["action"]))


DISPATCHERS = {fn.__name__: fn for fn in [*ALL_DISPATCHERS, *FORK_DISPATCHERS]}


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    s = VannaSettings(**overrides)
    monkeypatch.setattr("vanna_core.config.get_settings", lambda: s)


# ---------------------------------------------------------------- scopes


def test_write_actions_are_real_dispatcher_actions() -> None:
    for tool, actions in WRITE_ACTIONS.items():
        assert tool in DISPATCHERS, tool
        assert actions <= _actions(DISPATCHERS[tool]), (tool, actions - _actions(DISPATCHERS[tool]))


def test_write_only_tools_have_every_action_classified_as_write() -> None:
    for tool in WRITE_ONLY_TOOLS:
        assert WRITE_ACTIONS[tool] == _actions(DISPATCHERS[tool]), tool


def test_is_write_call() -> None:
    assert is_write_call("vanna_margin_trade", None)
    assert is_write_call("vanna_earn_write", "bogus")
    assert is_write_call("vanna_swap", "wallet_swap")
    assert not is_write_call("vanna_swap", "quote")
    assert not is_write_call("vanna_account", "health")
    assert is_write_call("vanna_account", "create")
    assert not is_write_call("vanna_oracle", "price")


def test_tool_cost_weights() -> None:
    assert tool_cost("vanna_margin_trade", "borrow") == 10
    assert tool_cost("vanna_swap", "margin_swap") == 10
    assert tool_cost("vanna_swap", "quote") == 1
    assert tool_cost("vanna_history", "margin") == 5
    assert tool_cost("vanna_oracle", "price") == 1


# ---------------------------------------------------------------- identity


def test_caller_scopes_parse_string_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, auth_enabled=True)
    tokens = identity.set_identity({"sub": "u1", "scope": "openid vanna:read"}, None)
    try:
        assert identity.caller_scopes() == {"openid", "vanna:read"}
        assert not identity.caller_has_write_scope()
    finally:
        identity.reset_identity(tokens)
    tokens = identity.set_identity({"sub": "u1", "scp": ["vanna:write"]}, None)
    try:
        assert identity.caller_has_write_scope()
    finally:
        identity.reset_identity(tokens)


def test_first_party_client_gets_write_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, auth_enabled=True, first_party_client_ids="copilot-app, other")
    tokens = identity.set_identity({"sub": "copilot-app", "azp": "copilot-app"}, None)
    try:
        assert identity.caller_has_write_scope()
    finally:
        identity.reset_identity(tokens)
    tokens = identity.set_identity({"sub": "x", "azp": "stranger"}, None)
    try:
        assert not identity.caller_has_write_scope()
    finally:
        identity.reset_identity(tokens)


def test_unauthenticated_write_scope_follows_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, auth_enabled=False)
    assert identity.caller_has_write_scope()
    _settings(monkeypatch, auth_enabled=True)
    assert not identity.caller_has_write_scope()


def test_request_identity_binds_only_stamped_scopes() -> None:
    class Req:
        def __init__(self, scope: dict[str, Any]) -> None:
            self.scope = scope

    scope: dict[str, Any] = {}
    identity.bind_identity_to_scope(scope, {"sub": "alice"}, "tok")
    with identity.request_identity(Req(scope)):
        assert identity.caller_sub() == "alice"
        assert identity.current_user_token() == "tok"
    assert identity.caller_sub() is None
    with identity.request_identity(None):
        assert identity.caller_sub() is None


# ---------------------------------------------------------------- token verifier


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _LocalVerifier(TokenVerifier):
    def __init__(self, key: rsa.RSAPrivateKey, issuer: str = ISSUER + "/") -> None:
        super().__init__(issuer=issuer, jwks_uri="https://unused.invalid/jwks", audience=AUDIENCE)
        self._public = key.public_key()

    def _signing_key(self, token: str) -> Any:
        return self._public


def _token(key: rsa.RSAPrivateKey, **claims: Any) -> str:
    body = {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1", "exp": int(time.time()) + 300, **claims}
    return jwt.encode(body, key, algorithm="RS256")


def test_verifier_accepts_valid_token_despite_trailing_slash_issuer(rsa_key: rsa.RSAPrivateKey) -> None:
    claims = _LocalVerifier(rsa_key).verify(_token(rsa_key))
    assert claims["sub"] == "user-1"


@pytest.mark.parametrize(
    "claims, message",
    [
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"aud": "https://other"}, "audience"),
        ({"iss": "https://evil"}, "issuer"),
    ],
)
def test_verifier_rejects(rsa_key: rsa.RSAPrivateKey, claims: dict[str, Any], message: str) -> None:
    with pytest.raises(AuthError, match=message):
        _LocalVerifier(rsa_key).verify(_token(rsa_key, **claims))


def test_verifier_rejects_foreign_signature(rsa_key: rsa.RSAPrivateKey) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError):
        _LocalVerifier(rsa_key).verify(_token(other))


# ---------------------------------------------------------------- auth middleware


def _echo_app(captured: dict[str, Any]):
    async def app(scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] == "lifespan":
            return
        captured["identity"] = identity.identity_from_scope(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def _auth_client(rsa_key: rsa.RSAPrivateKey, captured: dict[str, Any], origins: frozenset[str] = frozenset()):
    return TestClient(
        AuthMiddleware(_echo_app(captured), _LocalVerifier(rsa_key), origins, "https://mcp.example.com")
    )


def test_public_paths() -> None:
    assert is_public_path("/.well-known/oauth-protected-resource")
    assert is_public_path("/.well-known/oauth-authorization-server/mcp")
    assert is_public_path("/healthz")
    assert not is_public_path("/mcp")


def test_auth_lets_discovery_through_without_token(rsa_key: rsa.RSAPrivateKey) -> None:
    captured: dict[str, Any] = {}
    assert _auth_client(rsa_key, captured).get("/.well-known/oauth-protected-resource").status_code == 200


def test_auth_missing_bearer_is_401_with_metadata_pointer(rsa_key: rsa.RSAPrivateKey) -> None:
    r = _auth_client(rsa_key, {}).post("/mcp")
    assert r.status_code == 401
    assert "resource_metadata=\"https://mcp.example.com/.well-known/oauth-protected-resource\"" in r.headers[
        "www-authenticate"
    ]


def test_auth_invalid_token_is_401(rsa_key: rsa.RSAPrivateKey) -> None:
    r = _auth_client(rsa_key, {}).post("/mcp", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_auth_rejects_disallowed_origin(rsa_key: rsa.RSAPrivateKey) -> None:
    client = _auth_client(rsa_key, {}, frozenset({"https://app.vanna.finance"}))
    headers = {"Authorization": f"Bearer {_token(rsa_key)}", "Origin": "https://evil.example"}
    assert client.post("/mcp", headers=headers).status_code == 403
    headers["Origin"] = "https://app.vanna.finance"
    assert client.post("/mcp", headers=headers).status_code == 200


def test_auth_stamps_user_bearer_as_assertion(rsa_key: rsa.RSAPrivateKey) -> None:
    captured: dict[str, Any] = {}
    token = _token(rsa_key)
    _auth_client(rsa_key, captured).post("/mcp", headers={"Authorization": f"Bearer {token}"})
    claims, user_token = captured["identity"]
    assert claims["sub"] == "user-1"
    assert user_token == token


def test_auth_never_forwards_machine_bearer(rsa_key: rsa.RSAPrivateKey) -> None:
    captured: dict[str, Any] = {}
    token = _token(rsa_key, sub="app-client", azp="app-client")
    _auth_client(rsa_key, captured).post("/mcp", headers={"Authorization": f"Bearer {token}"})
    assert captured["identity"][1] is None

    _auth_client(rsa_key, captured).post(
        "/mcp", headers={"Authorization": f"Bearer {token}", "X-Vanna-User-Assertion": "user-assertion"}
    )
    assert captured["identity"][1] == "user-assertion"


# ---------------------------------------------------------------- rate limit


def test_token_bucket_and_breaker() -> None:
    bucket = TokenBucket(10, 1.0)
    now = time.monotonic()
    assert bucket.take(10, now) == 0
    assert bucket.take(1, now) == pytest.approx(1.0)
    assert bucket.take(1, now + 1.0) == 0

    breaker = CircuitBreaker(fail_threshold=2, cool_sec=60)
    breaker.record_failure("t")
    assert breaker.allow("t")
    breaker.record_failure("t")
    assert not breaker.allow("t")
    breaker.record_success("t")
    assert breaker.allow("t")


def test_rpc_tool_call_parsing() -> None:
    body = json.dumps({"method": "tools/call", "params": {"name": "vanna_swap", "arguments": {"action": "quote"}}})
    assert rpc_tool_call(body.encode()) == ("vanna_swap", "quote")
    assert rpc_tool_call(json.dumps({"method": "tools/list"}).encode()) == ("", None)
    assert rpc_tool_call(b"not json") == ("", None)


def test_caller_key_prefers_verified_identity() -> None:
    scope: dict[str, Any] = {"headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")], "client": ("9.9.9.9", 1)}
    assert caller_key(scope) == "ip:1.2.3.4"
    identity.bind_identity_to_scope(scope, {"sub": "alice"}, None)
    assert caller_key(scope) == "sub:alice"


def test_rate_limit_rejects_after_burst_and_replays_body() -> None:
    seen: list[bytes] = []

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        message = await receive()
        seen.append(message["body"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    client = TestClient(RateLimitMiddleware(inner, refill_per_sec=0.001, burst=10))
    write = json.dumps(
        {"method": "tools/call", "params": {"name": "vanna_earn_write", "arguments": {"action": "supply"}}}
    )
    assert client.post("/mcp", content=write).status_code == 200
    assert seen == [write.encode()]
    r = client.post("/mcp", content=write)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    assert client.post("/other", content=write).status_code == 200


def test_rate_limit_requires_burst_above_write_cost() -> None:
    with pytest.raises(ValueError):
        RateLimitMiddleware(lambda *_: None, burst=5)


# ---------------------------------------------------------------- scoped server over HTTP


def _scoped_app(rsa_key: rsa.RSAPrivateKey):
    from mcp_server.main import ScopedMCPServer

    server = ScopedMCPServer(name="scoped-test")

    async def vanna_earn_write(action: str) -> dict[str, Any]:
        return {"built": action}

    async def vanna_swap(action: str) -> dict[str, Any]:
        return {"ran": action}

    async def vanna_oracle(action: str) -> dict[str, Any]:
        return {"error": "internal_error", "message": "rpc down"}

    for fn in (vanna_earn_write, vanna_swap, vanna_oracle):
        server.add_tool(fn)
    inner = server.streamable_http_app(host="0.0.0.0", json_response=True)
    return AuthMiddleware(inner, _LocalVerifier(rsa_key), frozenset(), "https://mcp.example.com")


class _Session:
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
        session_id = self.last_headers.get("mcp-session-id")
        if session_id:
            self._headers["mcp-session-id"] = session_id
        self._headers["mcp-protocol-version"] = init["result"]["protocolVersion"]
        self.notify("notifications/initialized")

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        r = self._client.post("/mcp", headers=self._headers, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        self.last_headers = r.headers
        assert r.status_code == 200, (r.status_code, r.text)
        return r.json()

    def notify(self, method: str) -> None:
        self._client.post("/mcp", headers=self._headers, json={"jsonrpc": "2.0", "method": method})

    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}

    def call(self, name: str, action: str) -> dict[str, Any]:
        result = self.rpc("tools/call", {"name": name, "arguments": {"action": action}})["result"]
        return json.loads(result["content"][0]["text"])


def test_scoped_server_gates_writes_per_request(monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey) -> None:
    _settings(monkeypatch, auth_enabled=True)
    with TestClient(_scoped_app(rsa_key)) as client:
        reader = _Session(client, _token(rsa_key, scope="vanna:read"))
        assert reader.tool_names() == {"vanna_swap", "vanna_oracle"}
        assert reader.call("vanna_swap", "quote") == {"ran": "quote"}
        refused = reader.call("vanna_swap", "wallet_swap")
        assert refused["code"] == "insufficient_scope"
        assert reader.call("vanna_earn_write", "supply")["code"] == "insufficient_scope"

        writer = _Session(client, _token(rsa_key, sub="user-2", scope="vanna:read vanna:write"))
        assert writer.tool_names() == {"vanna_earn_write", "vanna_swap", "vanna_oracle"}
        assert writer.call("vanna_earn_write", "supply") == {"built": "supply"}


def test_scoped_server_opens_breaker_on_downstream_errors(
    monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey
) -> None:
    import mcp_server.main as main

    breaker = CircuitBreaker(fail_threshold=2, cool_sec=60)
    monkeypatch.setattr(main, "get_circuit_breaker", lambda: breaker)
    _settings(monkeypatch, auth_enabled=True)
    with TestClient(_scoped_app(rsa_key)) as client:
        session = _Session(client, _token(rsa_key, scope="vanna:read"))
        assert session.call("vanna_oracle", "price")["error"] == "internal_error"
        assert session.call("vanna_oracle", "price")["error"] == "internal_error"
        assert session.call("vanna_oracle", "price")["error"] == "tool_circuit_open"
