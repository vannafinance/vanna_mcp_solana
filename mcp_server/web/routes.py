"""The browser pages and JSON endpoints the user's own wallet talks to: plan approval and signing, and the wallet
connect step of the MCP login.

Everything here is a thin relay over the plan engine (`mcp_server/planning/engine.py`) and the OAuth consent service
(`mcp_server/oauth/consent.py`). The pages are static files that render server data with `textContent` (never
innerHTML), under a strict Content-Security-Policy; the endpoints are capability URLs — a plan id is a 256-bit secret,
and nothing moves without a signature from the plan's own wallet, which the server verifies itself.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_server.planning.engine import PlanError, get_engine

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
# The pages themselves: built from web/pages (`npm run build` there, and in the Dockerfile) into static/app/.
APP_DIR = STATIC_DIR / "app"
_STATIC_FILES = {
    "vanna-icon.png": "image/png", "vanna-wordmark.png": "image/png", "vanna-wordmark-white.png": "image/png",
    "agent-codex.png": "image/png", "plus-jakarta-sans.woff2": "font/woff2",
}
_APP_FILE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.(js|css)$")
MAX_BODY_BYTES = 16 * 1024  # a signed v0 transaction is at most ~1.7 KB of base64

# Scripts only from this origin; no inline script, no framing, no plugins, no third-party anything.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")
# With the email / social wallet on (DYNAMIC_ENVIRONMENT_ID), the pages also talk to Dynamic: its API and assets, the
# embedded wallet's signing frame and WebAssembly, and its injected styles. Still no inline script and no framing of
# these pages; everything else stays this origin only.
_DYNAMIC_HOSTS = "https://*.dynamicauth.com https://*.dynamic-static-assets.com"


def _csp(dynamic_rpc: str | None) -> str:
    if dynamic_rpc is None:
        return CSP
    rpc = f" {dynamic_rpc}" if dynamic_rpc else ""
    return (f"default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' {_DYNAMIC_HOSTS} wss://*.dynamicauth.com{rpc}; img-src 'self' data: {_DYNAMIC_HOSTS}; "
            f"frame-src {_DYNAMIC_HOSTS}; worker-src 'self' blob:; font-src 'self' data: {_DYNAMIC_HOSTS}; "
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

# An empty shell: the page is rendered by the app bundle from the values on #root (web/pages/src/lib/runtime.ts). Every
# value is escaped; the layout and theme are the Stellar MCP's pages (web/pages/src/components).
_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer"><title>{title}</title>
<link rel="icon" type="image/png" href="/static/vanna-icon.png"><link rel="stylesheet" href="/static/app/app.css?v={version}"></head>
<body class="min-h-full flex flex-col antialiased"><div id="root" class="flex min-h-screen flex-col" data-page="{page}" data-site="{site}" data-network="{network}" data-test="{test}" data-rpc="{rpc}"{dynamic}></div>
<noscript><p>This page needs JavaScript to talk to your wallet.</p></noscript>
<script type="module" src="/static/app/main.js?v={version}"></script></body></html>"""

_ENV_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def page_rpc(settings: Any) -> str:
    """This server's network for the page (Dynamic reads the Solana chain from it; transaction links point at it). An RPC
    URL carrying credentials (query string, user info) never reaches a page: then the page gets none."""
    rpc = urlparse(settings.solana_rpc_url)
    plain = rpc.scheme == "https" and rpc.netloc and not (rpc.query or rpc.username or rpc.password or rpc.fragment)
    return f"https://{rpc.netloc}{rpc.path}".rstrip("/") if plain else ""


def dynamic_page_config(settings: Any) -> tuple[str, str] | None:
    """(environment id, RPC URL for Dynamic) when the email wallet is on: DYNAMIC_ENVIRONMENT_ID is a Dynamic id."""
    env_id = settings.dynamic_environment_id.strip()
    if not env_id:
        return None
    if not _ENV_ID.match(env_id):
        logger.warning("DYNAMIC_ENVIRONMENT_ID is not a Dynamic environment id; the email wallet stays off")
        return None
    return env_id, page_rpc(settings)


_version: tuple[float, str] = (0.0, "")


def _build_version() -> str:
    """A short hash of the built main.js and app.css, in their URLs, so a new build is never served from a stale cache."""
    global _version
    mtime = max((APP_DIR / n).stat().st_mtime for n in ("main.js", "app.css"))
    if _version[0] != mtime:
        digest = hashlib.sha256((APP_DIR / "main.js").read_bytes() + (APP_DIR / "app.css").read_bytes()).hexdigest()[:12]
        _version = (mtime, digest)
    return _version[1]


def _page(title: str, page: str) -> Response:
    from mcp_server.web.setup import sign_base_url
    from vanna_core.config import get_settings

    if not ((APP_DIR / "main.js").is_file() and (APP_DIR / "app.css").is_file()):
        logger.error("The pages are not built (web/pages: npm ci && npm run build)")
        return HTMLResponse("<!doctype html><title>Vanna</title><p>This page is not available right now.</p>", status_code=503,
                            headers=_PAGE_HEADERS)
    settings = get_settings()
    dynamic = dynamic_page_config(settings)
    network = "Test network (test funds only)" if settings.is_fork else f"Solana {settings.solana_cluster_label}"
    attrs = f' data-dynamic-env="{html.escape(dynamic[0])}"' if dynamic else ""
    body = _SHELL.format(title=html.escape(title), page=page, site=html.escape(urlparse(sign_base_url(settings)).netloc),
                         network=html.escape(network), test="1" if settings.is_fork else "0",
                         rpc=html.escape(page_rpc(settings)), dynamic=attrs, version=_build_version())
    return HTMLResponse(body, headers={**_PAGE_HEADERS, "Content-Security-Policy": _csp(dynamic[1] if dynamic else None)})


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


async def _app_static(request: Request) -> Response:
    """The built pages: main.js, its chunks (content-hashed names) and app.css."""
    name = request.path_params["name"]
    path = APP_DIR / name
    if not _APP_FILE.match(name) or not path.is_file():
        return Response(status_code=404)
    kind = "text/css" if name.endswith(".css") else "application/javascript"
    # Chunks are named by their content and main.js / app.css are linked with their build hash (`_build_version`).
    cache = "public, max-age=31536000, immutable" if name.startswith("chunk-") or request.query_params.get("v") else "no-cache"
    headers = {"Cache-Control": cache, "X-Content-Type-Options": "nosniff", "Vary": "Accept-Encoding"}
    # The build writes brotli and gzip copies beside each file: send the smallest one the browser accepts.
    accepted = {e.split(";")[0].strip().lower() for e in request.headers.get("accept-encoding", "").split(",")}
    for encoding, suffix in (("br", ".br"), ("gzip", ".gz")):
        packed = APP_DIR / f"{name}{suffix}"
        if encoding in accepted and packed.is_file():
            return FileResponse(packed, media_type=kind, headers={**headers, "Content-Encoding": encoding})
    return FileResponse(path, media_type=kind, headers=headers)


async def _plan_page(_request: Request) -> Response:
    return _page("Review · Vanna", "plan")


async def _sign_alias(request: Request) -> Response:
    return RedirectResponse(f"/plan/{request.path_params['plan_id']}", status_code=302, headers=_NO_STORE)


async def _connect_page(_request: Request) -> Response:
    return _page("Sign in · Vanna", "connect")


def build_web_routes() -> list[Route]:
    return [
        Route("/plan/{plan_id}", _plan_page, methods=["GET"]),
        Route("/sign/{plan_id}", _sign_alias, methods=["GET"]),
        Route("/connect", _connect_page, methods=["GET"]),
        Route("/static/app/{name}", _app_static, methods=["GET"]),
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
