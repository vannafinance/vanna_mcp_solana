"""Per-request caller identity for the Vanna Solana MCP server.

AuthMiddleware (mcp_server/auth.py) verifies the bearer token and stamps the
verified claims, plus the end-user assertion (unverified; never authorizes a
transaction), onto the request's own ASGI scope. The tool handlers in mcp_server/main.py
read that scope through `ServerRequestContext.request` and bind it to the
ContextVars below for the duration of one tools/list or tools/call.

Why the scope and not only a ContextVar set in the middleware: under stateful
streamable HTTP, `StreamableHTTPSessionManager` starts one long-lived task per
session from the `initialize` request, and every later tools/call is
dispatched by that task. A ContextVar set by the middleware for a later
request is invisible to it, so the tool would see whoever opened the session.
The scope travels with the exact request it belongs to.

With AUTH_ENABLED=false nothing is ever stamped, every reader returns None,
and `caller_has_write_scope()` is True — the stdio / local-dev / test path.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator

_current_claims: ContextVar[dict[str, Any] | None] = ContextVar("vanna_current_claims", default=None)
# The end-user assertion the caller forwarded (never logged).
_current_user_token: ContextVar[str | None] = ContextVar("vanna_current_user_token", default=None)

_SCOPE_KEY = "vanna.identity"

WRITE_SCOPES = frozenset({"vanna:write", "write", "*", "vanna:*"})


class IdentityError(RuntimeError):
    """Raised by require_caller_sub() when a caller identity is required but absent."""


def bind_identity_to_scope(scope: dict[str, Any], claims: dict[str, Any] | None, token: str | None) -> None:
    """Stamp this request's verified identity onto its own ASGI scope."""
    scope[_SCOPE_KEY] = (claims, token)


def identity_from_scope(scope: Any) -> tuple[dict[str, Any] | None, str | None] | None:
    """The identity AuthMiddleware stamped onto `scope`, or None if unstamped."""
    if not isinstance(scope, dict):
        return None
    stamped = scope.get(_SCOPE_KEY)
    if not isinstance(stamped, tuple) or len(stamped) != 2:
        return None
    return stamped


def set_identity(
    claims: dict[str, Any] | None, token: str | None
) -> tuple[Token[dict[str, Any] | None], Token[str | None]]:
    """Bind claims + user token to the current context; pass the result to reset_identity."""
    return (_current_claims.set(claims), _current_user_token.set(token))


def reset_identity(tokens: tuple[Token[dict[str, Any] | None], Token[str | None]]) -> None:
    claims_token, user_token = tokens
    _current_claims.reset(claims_token)
    _current_user_token.reset(user_token)


@contextmanager
def request_identity(request: Any) -> Iterator[None]:
    """Bind the identity stamped on an HTTP request for the enclosed block.

    `request` is `ServerRequestContext.request` — a Starlette Request under
    streamable HTTP, None under stdio. An unstamped request leaves the current
    context untouched.
    """
    stamped = identity_from_scope(getattr(request, "scope", None))
    if stamped is None:
        yield
        return
    tokens = set_identity(*stamped)
    try:
        yield
    finally:
        reset_identity(tokens)


def current_claims() -> dict[str, Any] | None:
    return _current_claims.get()


def current_user_token() -> str | None:
    return _current_user_token.get()


def caller_sub() -> str | None:
    claims = current_claims()
    if not claims:
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


def require_caller_sub() -> str:
    sub = caller_sub()
    if sub is None:
        raise IdentityError("No authenticated caller identity in this request context.")
    return sub


def caller_client_id() -> str | None:
    claims = current_claims()
    if not claims:
        return None
    cid = claims.get("client_id") or claims.get("azp") or claims.get("cid")
    if isinstance(cid, str) and cid.strip():
        return cid.strip()
    return None


def first_party_client_ids() -> frozenset[str]:
    from vanna_core.config import get_settings

    raw = get_settings().first_party_client_ids
    return frozenset(cid.strip() for cid in raw.split(",") if cid.strip())


def caller_scopes() -> set[str] | None:
    """Scopes granted by the caller's token, or None when unauthenticated.

    First-party client IDs (FIRST_PARTY_CLIENT_IDS) are granted vanna:read +
    vanna:write regardless of the token's scope claim, since some IdPs issue
    tokens without one.
    """
    claims = current_claims()
    if claims is None:
        return None
    raw = claims.get("scope") or claims.get("scp")
    if isinstance(raw, str):
        scopes = {s.strip() for s in raw.split() if s.strip()}
    elif isinstance(raw, (list, tuple, set)):
        scopes = {str(s).strip() for s in raw if str(s).strip()}
    else:
        scopes = set()
    cid = caller_client_id()
    if cid and cid in first_party_client_ids():
        scopes |= {"vanna:read", "vanna:write"}
    return scopes


def caller_has_write_scope() -> bool:
    """True if the caller may run write actions (see mcp_server/scopes.py)."""
    scopes = caller_scopes()
    if scopes is not None:
        return bool(scopes & WRITE_SCOPES)
    from vanna_core.config import get_settings

    return not get_settings().auth_enabled
