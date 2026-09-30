"""Dev-only fork faucet — a thin proxy to `vanna-credit-backend`'s own
`POST /api/faucet` (Surfpool cheatcode, not a real transaction). Backs
`vanna_fork.faucet` (TOOLS_CATALOGUE.md §3.15). Never registered unless
`ENABLE_FORK_TOOLS=true`, and never part of the Copilot's allowlist even
then (TOOLS_CATALOGUE.md §6.2 item 6).

The backend's faucet endpoint is unauthenticated today; once hardened with a
shared secret, `PRICE_KEEPER_SECRET` should be sent here too (same host as
the `dev-fork` oracle adapter's keeper).
"""

from __future__ import annotations

import httpx

from vanna_core.config import VannaSettings
from vanna_core.types import ContractCallError


class ForkContract:
    def __init__(self, settings: VannaSettings) -> None:
        self._settings = settings

    async def faucet(self, address: str, symbol: str | None = None) -> dict:
        if not self._settings.enable_fork_tools:
            raise ContractCallError("vanna_fork is disabled (ENABLE_FORK_TOOLS is not set)")

        payload: dict = {"address": address}
        if symbol is not None:
            payload["symbol"] = symbol
        headers = {"Content-Type": "application/json"}
        if self._settings.price_keeper_secret:
            headers["Authorization"] = f"Bearer {self._settings.price_keeper_secret}"

        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(f"{self._settings.price_keeper_url}/api/faucet", json=payload, headers=headers)
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 — a non-JSON response (e.g. a 404 HTML page)
            raise ContractCallError(
                f"Faucet request failed: HTTP {resp.status_code}, non-JSON response ({exc})"
            ) from exc
        if resp.status_code != 200 or "error" in body:
            raise ContractCallError(f"Faucet request failed: {body.get('error', body)}")
        return body
