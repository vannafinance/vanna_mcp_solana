"""In-process calls to the MCP's own dispatchers.

The planner and the safety layer do what the website Copilot does — read live state, size an amount, ask the
program's own pre-flight checks, then build — and the Copilot does all of that by calling this server's tools.
Doing the same through the dispatcher functions (rather than re-deriving anything) keeps one source of truth for
every number and every rule, and makes the port from the Copilot faithful.
"""

from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """A dispatcher returned {"error": code, "message": ...} (the Copilot's `MCPError`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _registry() -> dict[str, Any]:
    from mcp_server.dispatchers import ALL_DISPATCHERS

    return {fn.__name__: fn for fn in ALL_DISPATCHERS}


async def call_raw(tool: str, **args: Any) -> dict[str, Any]:
    """Run a dispatcher and return its dict as-is (a failed write envelope still carries its simulation)."""
    fn = _registry().get(tool)
    if fn is None:
        raise ToolError("invalid_input", f"Unknown tool {tool!r}.")
    result = await fn(**args)
    if not isinstance(result, dict):
        raise ToolError("internal_error", f"{tool} returned an unexpected result.")
    return result


async def call(tool: str, **args: Any) -> dict[str, Any]:
    """Run a dispatcher; raise `ToolError` when it reports an error instead of a result."""
    result = await call_raw(tool, **args)
    if isinstance(result.get("error"), str):
        raise ToolError(result["error"], str(result.get("message") or result["error"]))
    return result
