"""The email / social wallet (Dynamic) on the connect and plan pages: when it is offered, what the page carries, and the CSP."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_server.web import routes

ENV_ID = "90dad26e-0000-4000-8000-000000000000"


def _settings(env_id: str = ENV_ID, rpc: str = "https://rpc-devnet.solana.vanna.finance") -> SimpleNamespace:
    return SimpleNamespace(dynamic_environment_id=env_id, solana_rpc_url=rpc)


@pytest.fixture
def built(tmp_path, monkeypatch):
    (tmp_path / "main.js").write_text("// main")
    (tmp_path / "app.css").write_text("/* css */")
    monkeypatch.setattr(routes, "APP_DIR", tmp_path)
    return tmp_path


def _with_env(monkeypatch, env_id: str) -> None:
    from vanna_core import config

    real = config.get_settings()
    monkeypatch.setattr(config, "get_settings", lambda: real.model_copy(update={"dynamic_environment_id": env_id}))


def test_off_without_an_environment_id():
    assert routes.dynamic_page_config(_settings(env_id="")) is None


def test_off_with_a_malformed_environment_id():
    assert routes.dynamic_page_config(_settings(env_id="not-an-id\" onload=\"x")) is None


def test_on_points_dynamic_at_this_network():
    assert routes.dynamic_page_config(_settings()) == (ENV_ID, "https://rpc-devnet.solana.vanna.finance")


@pytest.mark.parametrize("rpc", [
    "https://mainnet.helius-rpc.com/?api-key=secret",
    "https://user:pass@rpc.example.com",
    "http://rpc.example.com",
])
def test_an_rpc_with_credentials_never_reaches_the_page(rpc):
    env_id, page_rpc = routes.dynamic_page_config(_settings(rpc=rpc))
    assert env_id == ENV_ID and page_rpc == ""
    assert routes.page_rpc(_settings(rpc=rpc)) == ""


def test_csp_stays_strict_without_the_email_wallet():
    assert routes._csp(None) == routes.CSP
    assert "dynamicauth" not in routes.CSP and "'unsafe-inline'" not in routes.CSP


def test_csp_with_the_email_wallet_opens_only_dynamic_and_the_rpc():
    csp = routes._csp("https://rpc-devnet.solana.vanna.finance")
    assert "script-src 'self' 'wasm-unsafe-eval';" in csp  # no inline or third-party script
    assert "frame-ancestors 'none'" in csp and "form-action 'none'" in csp
    assert "https://rpc-devnet.solana.vanna.finance" in csp
    assert "https://*.dynamicauth.com" in csp
    no_rpc = routes._csp("")
    assert "connect-src 'self' https://*.dynamicauth.com https://*.dynamic-static-assets.com wss://*.dynamicauth.com;" in no_rpc


def test_the_page_carries_the_environment(built, monkeypatch):
    _with_env(monkeypatch, ENV_ID)
    page = routes._page("t", "connect")
    body = page.body.decode()
    assert f'data-dynamic-env="{ENV_ID}"' in body and 'data-page="connect"' in body
    assert "dynamicauth" in page.headers["content-security-policy"]


def test_the_page_without_the_email_wallet(built, monkeypatch):
    _with_env(monkeypatch, "")
    page = routes._page("t", "plan")
    body = page.body.decode()
    assert "data-dynamic-env" not in body
    assert page.headers["content-security-policy"] == routes.CSP
