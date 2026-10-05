"""Compatibility test: a real MCP SDK OAuth client logs in to the public connector exactly as Claude/Cursor would.

The SDK's `OAuthClientProvider` does discovery (RFC 9728 -> RFC 8414), dynamic client registration, PKCE,
the authorization redirect and the token exchange by itself. The only thing played by hand is the browser:
the "redirect handler" does what the Vanna app's consent page does (connect a wallet, sign the SIWS
message the server issued) against a real uvicorn server. No RPC, no network beyond loopback."""

from __future__ import annotations

import base64
import socket
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp_types import TextContent
from mcp.shared.auth import AuthorizationCodeResult
from solders.keypair import Keypair

import mcp_server.main as main
from mcp_server.oauth.store import MemoryRecordStore
from tests.test_oauth_connector import _settings, _stub_server

CLIENT_REDIRECT = "http://127.0.0.1:9/callback"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Storage:
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


@pytest.fixture
def live_connector(monkeypatch: pytest.MonkeyPatch):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    s = _settings(
        external_base_url=base,
        oauth_consent_url=f"http://127.0.0.1:{port}/vanna-consent",
        mcp_http_host="127.0.0.1",
        mcp_http_port=port,
    )
    monkeypatch.setattr(main, "settings", s)
    monkeypatch.setattr("vanna_core.config.get_settings", lambda: s)
    calls: list[dict[str, Any]] = []
    app = main.build_public_app(store=MemoryRecordStore(), server=_stub_server(calls))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    try:
        yield base, calls
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def test_sdk_oauth_client_logs_in_with_a_wallet_and_calls_tools(live_connector) -> None:
    base, calls = live_connector
    wallet = Keypair()
    outcome: dict[str, Any] = {}

    async def redirect_handler(authorization_url: str) -> None:
        """The browser + consent page: follow /authorize, sign in with the wallet, follow the redirect back."""
        consent_origin = base
        async with httpx.AsyncClient(follow_redirects=False) as browser:
            first = await browser.get(authorization_url)
            assert first.status_code == 302, first.text
            consent = urlparse(first.headers["location"])
            assert consent.path == "/vanna-consent"
            req = parse_qs(consent.query)["req"][0]
            headers = {"Origin": consent_origin}

            shown = (await browser.get(f"{base}/oauth/consent/request", params={"req": req}, headers=headers)).json()
            outcome["shown"] = shown

            ch = (
                await browser.post(
                    f"{base}/oauth/consent/challenge",
                    json={"req": req, "wallet": str(wallet.pubkey())},
                    headers=headers,
                )
            ).json()
            signature = base64.b64encode(bytes(wallet.sign_message(ch["message"].encode()))).decode()
            done = await browser.post(
                f"{base}/oauth/consent/complete",
                json={"req": req, "wallet": str(wallet.pubkey()), "nonce": ch["nonce"], "signature": signature},
                headers=headers,
            )
            assert done.status_code == 200, done.text
            query = parse_qs(urlparse(done.json()["redirect_url"]).query)
            outcome["code"] = AuthorizationCodeResult(
                code=query["code"][0], state=query.get("state", [None])[0], iss=query.get("iss", [None])[0]
            )

    async def callback_handler() -> AuthorizationCodeResult:
        return outcome["code"]

    storage = _Storage()
    provider = OAuthClientProvider(
        server_url=f"{base}/mcp",
        client_metadata=OAuthClientMetadata(
            client_name="SDK Test Client",
            redirect_uris=[CLIENT_REDIRECT],  # type: ignore[list-item]
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="vanna:read vanna:write",
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    async with httpx2.AsyncClient(auth=provider, follow_redirects=True) as http:
        async with streamable_http_client(f"{base}/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert {"vanna_earn_write", "vanna_margin_status"} <= tools

                # The wallet comes from the token: no `authority` / `owner` is sent.
                result = await session.call_tool("vanna_margin_status", {"action": "health"})
                block = result.content[0]
                assert isinstance(block, TextContent) and str(wallet.pubkey()) in block.text
                built = await session.call_tool("vanna_earn_write", {"action": "supply"})
                assert calls[-1]["owner"] == str(wallet.pubkey()), built

                # A different wallet is refused even though the model asked nicely.
                other = str(Keypair().pubkey())
                refused = await session.call_tool("vanna_earn_write", {"action": "supply", "owner": other})
                text = refused.content[0].text  # type: ignore[union-attr]
                assert "wallet_mismatch" in text and all(c.get("owner") != other for c in calls)

    assert outcome["shown"]["client_name"] == "SDK Test Client"
    assert outcome["shown"]["redirect_host"] == "127.0.0.1:9"
    assert storage.tokens is not None and storage.tokens.refresh_token
    assert storage.client_info is not None and storage.client_info.token_endpoint_auth_method == "none"
