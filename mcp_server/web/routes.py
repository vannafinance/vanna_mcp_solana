"""The browser pages and JSON endpoints the user's own wallet talks to: plan approval and signing, and the wallet
connect step of the MCP login.

Everything here is a thin relay over the plan engine (`mcp_server/planning/engine.py`) and the OAuth consent service
(`mcp_server/oauth/consent.py`). The pages are static files that render server data with `textContent` (never
innerHTML), under a strict Content-Security-Policy; the endpoints are capability URLs — a plan id is a 256-bit secret,
and nothing moves without a signature from the plan's own wallet, which the server verifies itself.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_server.planning.engine import PlanError, get_engine

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_FILES = {
    "vanna.css": "text/css", "wallet.js": "application/javascript", "plan.js": "application/javascript",
    "connect.js": "application/javascript", "solana-web3-1.98.4.min.js": "application/javascript",
}
MAX_BODY_BYTES = 16 * 1024  # a signed v0 transaction is at most ~1.7 KB of base64

# Scripts only from this origin; no inline script, no framing, no third-party anything.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_PAGE_HEADERS = {**_NO_STORE, "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}

_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{title}</title><link rel="stylesheet" href="/static/vanna.css"></head>
<body><main><header><div class="logo"></div><h1>{heading}</h1></header>{body}</main>
<script src="/static/solana-web3-1.98.4.min.js"></script><script src="/static/wallet.js"></script><script src="/static/{script}"></script></body></html>"""

_PLAN_BODY = """<div id="msg" class="notice" hidden></div>
<div class="card"><div class="row"><span id="status"></span></div><h2 id="summary" class="mt"></h2>
<div class="muted">Wallet <span id="wallet" class="mono"></span></div><div id="sim" class="muted mt"></div>
<ul id="warnings" class="plain muted"></ul><ol id="steps" class="steps"></ol></div>
<div id="current" class="card" hidden></div>
<div class="row"><button id="approve" hidden>Approve plan with wallet</button><button id="run" hidden>Start / continue</button><button id="cancel" class="secondary" hidden>Cancel plan</button></div>
<p class="muted">Vanna never holds your keys. Your wallet shows each transaction before you sign it, and nothing is sent without your signature.</p>"""

_CONNECT_BODY = """<div id="msg" class="notice" hidden></div>
<div id="panel" class="card" hidden><h2>Connect your wallet to <span id="client"></span></h2>
<p class="muted">This app will return to <strong id="host" class="mono"></strong>. Only continue if you started this connection and recognise it.</p>
<p class="muted">It will be able to:</p><ul id="scopes" class="plain"></ul>
<p class="muted">It can never sign or send anything: every transaction still needs your approval in your wallet. Signing the next message proves you own the wallet and costs nothing.</p>
<div class="row"><button id="allow">Connect wallet and approve</button><button id="deny" class="secondary">Decline</button></div></div>"""


def _page(title: str, heading: str, body: str, script: str) -> HTMLResponse:
    return HTMLResponse(_SHELL.format(title=title, heading=heading, body=body, script=script), headers=_PAGE_HEADERS)


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


def _guard(handler):
    async def endpoint(request: Request) -> Response:
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


async def _next(request: Request) -> dict[str, Any]:
    return await _engine().build_next(request.path_params["plan_id"])


async def _submit(request: Request) -> dict[str, Any]:
    body = await _body(request)
    return await _engine().submit(request.path_params["plan_id"], str(body.get("tx_id", "")), str(body.get("signed", "")))


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
    """Paths served without a bearer: the pages are capability URLs and the wallet signature is the authority."""
    return path in ("/connect",) or path.startswith(("/plan/", "/sign/", "/static/", "/api/plan/"))
