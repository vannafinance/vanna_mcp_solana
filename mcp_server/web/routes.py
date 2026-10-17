"""The browser pages and JSON endpoints the user's own wallet talks to: plan approval and signing, and the wallet
connect step of the MCP login.

Everything here is a thin relay over the plan engine (`mcp_server/planning/engine.py`) and the OAuth consent service
(`mcp_server/oauth/consent.py`). The pages are static files that render server data with `textContent` (never
innerHTML), under a strict Content-Security-Policy; the endpoints are capability URLs — a plan id is a 256-bit secret,
and nothing moves without a signature from the plan's own wallet, which the server verifies itself.
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_server.planning.engine import PlanError, get_engine

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_FILES = {
    "vanna.css": "text/css", "wallet.js": "application/javascript", "plan.js": "application/javascript",
    "connect.js": "application/javascript", "solana-web3-1.98.4.min.js": "application/javascript",
    "vanna-icon.png": "image/png",
}
MAX_BODY_BYTES = 16 * 1024  # a signed v0 transaction is at most ~1.7 KB of base64

# Scripts only from this origin; no inline script, no framing, no plugins, no third-party anything.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
# HSTS only takes effect over https (the hosted service); a local http run ignores it. COOP keeps other sites from
# holding a handle on this window but still lets a wallet open its own popup.
_PAGE_HEADERS = {**_NO_STORE, "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                 "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                 "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
                 "Cross-Origin-Resource-Policy": "same-origin",
                 "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), "
                                       "bluetooth=(), hid=(), interest-cohort=()"}

_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer"><title>{title}</title>
<link rel="icon" type="image/png" href="/static/vanna-icon.png"><link rel="stylesheet" href="/static/vanna.css"></head>
<body><main id="app" data-site="{site}" data-network="{network}" data-test="{test}">
<header><img class="logo" src="/static/vanna-icon.png" alt="" width="36" height="36"><div class="brand"><div class="name">Vanna Finance</div><div class="muted small">{heading}</div></div><span id="network" class="badge" hidden></span></header>
<div id="site-bad" class="notice bad" hidden></div>{body}
<footer class="muted small"><span class="lock"></span><span><span id="site-host" class="mono"></span> · Vanna never holds your keys. Your wallet shows every transaction before you sign it.</span></footer></main>
<script src="/static/solana-web3-1.98.4.min.js"></script><script src="/static/wallet.js"></script><script src="/static/{script}"></script></body></html>"""

_PICKER = """<div id="picker" class="card" hidden><h2>Choose your wallet</h2><div id="picker-list" class="wallets"></div>
<div id="picker-mobile" hidden><p class="muted">No wallet in this browser. On a phone, open this page in your wallet's browser:</p><div id="picker-links" class="wallets"></div></div>
<div class="row mt"><button id="picker-cancel" class="secondary">Back</button></div></div>"""

_PLAN_BODY = """<div id="loading" class="card muted">Loading…</div>
<section id="plan" class="card" hidden>
<div class="row spread"><span id="status"></span><span id="countdown" class="small muted"></span></div>
<h2 id="summary" class="title"></h2>
<div id="hf" class="hf" hidden><div><div class="muted small">Health factor now</div><div id="hf-now" class="big"></div></div>
<div class="arrow muted">→</div><div><div class="muted small">After (worst case)</div><div id="hf-after" class="big"></div></div></div>
<ol id="steps" class="steps"></ol>
<details id="details" class="details"><summary>Details and risks</summary><ul id="warnings" class="plain muted small"></ul>
<div class="kv small"><span class="muted">Wallet</span><span id="wallet" class="mono"></span></div></details>
</section>
<div id="msg" class="notice" hidden></div>
<div id="current" class="card focus" hidden></div>""" + _PICKER + """
<div class="actions"><button id="primary" hidden></button><button id="cancel" class="link" hidden>Cancel plan</button></div>"""

_CONNECT_BODY = """<div id="msg" class="notice" hidden></div>
<div id="panel" class="card" hidden><h2>Connect your wallet to <span id="client"></span></h2>
<p class="muted">This app will return to <strong id="host" class="mono"></strong>. Only continue if you started this connection and recognise it.</p>
<p class="muted">It will be able to:</p><ul id="scopes" class="plain"></ul>
<p class="muted">It can never sign or send anything: every transaction still needs your approval in your wallet. Signing the next message proves you own the wallet and costs nothing.</p>
<div class="row actions"><button id="allow">Connect wallet and approve</button><button id="deny" class="secondary">Decline</button></div></div>""" + _PICKER


def _shell_values() -> dict[str, str]:
    """What the page needs to know about where it should live: the signing host and the network, from the settings."""
    from mcp_server.web.setup import sign_base_url
    from vanna_core.config import get_settings

    settings = get_settings()
    site = urlparse(sign_base_url(settings)).netloc
    network = "Test network (test funds only)" if settings.is_fork else f"Solana {settings.solana_cluster_label}"
    return {"site": html.escape(site), "network": html.escape(network), "test": "1" if settings.is_fork else "0"}


def _page(title: str, heading: str, body: str, script: str) -> HTMLResponse:
    page = _SHELL.format(title=title, heading=heading, body=body, script=script, **_shell_values())
    return HTMLResponse(page, headers=_PAGE_HEADERS)


def _json(payload: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=_NO_STORE)


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise PlanError("invalid_request", "Request body too large.", 413)
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise PlanError("invalid_request", "Body must be JSON.") from exc
    if not isinstance(data, dict):
        raise PlanError("invalid_request", "Body must be a JSON object.")
    return data


def _engine():
    engine = get_engine()
    if engine is None:
        raise PlanError("not_available", "Signing pages are not enabled on this server.", 503)
    return engine


def _same_origin(request: Request) -> bool:
    """A browser names the page that sent a POST in `Origin`; only this site's own pages may approve, prepare, submit
    or cancel. A request without `Origin` is not a cross-site browser request (a browser always sends it on POST)."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    return origin != "null" and urlparse(origin).netloc.lower() == request.headers.get("host", "").lower()


def _guard(handler):
    async def endpoint(request: Request) -> Response:
        if request.method == "POST" and not _same_origin(request):
            return _json({"error": "forbidden_origin", "message": "This request did not come from the Vanna signing page."}, 403)
        try:
            return _json(await handler(request))
        except PlanError as exc:
            return _json({"error": exc.code, "message": exc.message}, exc.status)
        except Exception:
            logger.exception("web endpoint failed")
            return _json({"error": "server_error", "message": "Something went wrong."}, 500)

    return endpoint


async def _view(request: Request) -> dict[str, Any]:
    return await _engine().get(request.path_params["plan_id"])


async def _approve(request: Request) -> dict[str, Any]:
    body = await _body(request)
    return await _engine().approve(request.path_params["plan_id"], str(body.get("wallet", "")), str(body.get("signature", "")))


def _run_token(request: Request) -> str:
    """The run token the approval returned to this page. Always a string, so the engine always checks it."""
    return request.headers.get("x-plan-token", "")[:128]


async def _next(request: Request) -> dict[str, Any]:
    return await _engine().build_next(request.path_params["plan_id"], _run_token(request))


async def _submit(request: Request) -> dict[str, Any]:
    body = await _body(request)
    return await _engine().submit(request.path_params["plan_id"], str(body.get("tx_id", "")), str(body.get("signed", "")),
                                  _run_token(request))


async def _cancel(request: Request) -> dict[str, Any]:
    return await _engine().cancel(request.path_params["plan_id"])


async def _static(request: Request) -> Response:
    name = request.path_params["name"]
    kind = _STATIC_FILES.get(name)
    if kind is None:
        return Response(status_code=404)
    return FileResponse(STATIC_DIR / name, media_type=kind, headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"})


async def _plan_page(_request: Request) -> Response:
    return _page("Vanna — review and sign", "Review and sign", _PLAN_BODY, "plan.js")


async def _sign_alias(request: Request) -> Response:
    return RedirectResponse(f"/plan/{request.path_params['plan_id']}", status_code=302, headers=_NO_STORE)


async def _connect_page(_request: Request) -> Response:
    return _page("Vanna — connect your wallet", "Connect your wallet", _CONNECT_BODY, "connect.js")


def build_web_routes() -> list[Route]:
    return [
        Route("/plan/{plan_id}", _plan_page, methods=["GET"]),
        Route("/sign/{plan_id}", _sign_alias, methods=["GET"]),
        Route("/connect", _connect_page, methods=["GET"]),
        Route("/static/{name}", _static, methods=["GET"]),
        Route("/api/plan/{plan_id}", _guard(_view), methods=["GET"]),
        Route("/api/plan/{plan_id}/approve", _guard(_approve), methods=["POST"]),
        Route("/api/plan/{plan_id}/next", _guard(_next), methods=["POST"]),
        Route("/api/plan/{plan_id}/submit", _guard(_submit), methods=["POST"]),
        Route("/api/plan/{plan_id}/cancel", _guard(_cancel), methods=["POST"]),
    ]


def is_web_public_path(path: str) -> bool:
    """Paths served without a bearer: the link lets its holder read or cancel a plan; approving takes the wallet's
    signature, and preparing or submitting a step the run token that approval returned to the page."""
    return path in ("/connect",) or path.startswith(("/plan/", "/sign/", "/static/", "/api/plan/"))
