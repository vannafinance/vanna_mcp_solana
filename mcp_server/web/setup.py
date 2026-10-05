"""Turns the signing pages and the plan engine on for this process.

The pages are served by this same process (HTTP transports) or, for a stdio run, by a small local web server on a
background thread — either way the plan engine and the tools share one in-process state, so a plan the model draws is
the plan the user's browser opens.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from mcp_server import safety as safety_module
from mcp_server.oauth.store import MemoryRecordStore, RecordStore
from mcp_server.planning.engine import PlanEngine, get_engine, set_engine
from mcp_server.safety import Safety, set_safety
from mcp_server.signing import rpc_sender, rpc_status_reader
from vanna_core.config import VannaSettings

logger = logging.getLogger(__name__)


def sign_base_url(settings: VannaSettings) -> str:
    """Where the user's browser reaches the pages: SIGN_BASE_URL, else EXTERNAL_BASE_URL, else the local bind."""
    explicit = settings.sign_base_url.strip().rstrip("/")
    if explicit:
        return explicit
    external = settings.external_base_url.strip().rstrip("/")
    if external:
        return external
    host = "127.0.0.1" if settings.mcp_http_host in ("0.0.0.0", "::", "") else settings.mcp_http_host
    return f"http://{host}:{settings.mcp_http_port}"


def configure_signing(settings: VannaSettings, store: RecordStore | None = None) -> PlanEngine:
    """Create the plan engine and the safety counters on `store` (shared with the OAuth state in public mode)."""
    store = store or MemoryRecordStore()
    safety = Safety(store)
    set_safety(safety)
    engine = PlanEngine(
        store, safety, rpc_sender(settings.solana_rpc_url), rpc_status_reader(settings.solana_rpc_url),
        base_url=lambda: sign_base_url(settings),
    )
    set_engine(engine)
    logger.info("Signing pages enabled — base URL %s (safety layer %s)", sign_base_url(settings),
                "on" if safety_module.active() else "off")
    return engine


def start_local_web_server(settings: VannaSettings) -> threading.Thread:
    """stdio runs: serve the signing pages on a background thread (127.0.0.1 only)."""
    import uvicorn
    from starlette.applications import Starlette

    from mcp_server.web.routes import build_web_routes

    app = Starlette(routes=build_web_routes())
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=settings.mcp_http_port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, name="vanna-web", daemon=True)
    thread.start()
    logger.info("Signing pages listening on %s", sign_base_url(settings))
    return thread


def attach_to_app(app: Any) -> None:
    """Mount the pages on a Starlette app (before any middleware wraps it)."""
    from mcp_server.web.routes import build_web_routes

    app.router.routes[0:0] = build_web_routes()
