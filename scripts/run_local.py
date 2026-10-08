"""Run the Vanna Solana MCP locally for your own testing: `python scripts/run_local.py`.

Starts the server over HTTP on 127.0.0.1 (default port 8000, override with PORT) with everything a person needs to use
it from Claude Code, Cursor or any MCP client, against the hosted Surfpool fork:

  * the product safety layer (1.15 health floor, per-action / per-day limits, build rate) — the Copilot's guardrails;
  * the signing pages: a write returns a link; you open it, approve and sign with YOUR wallet (Phantom/Backpack/Solflare);
  * the test faucet (`vanna_wallet action=faucet`) so a fresh wallet can be funded on the fork.

Nothing here signs or sends for you. Every value below is a default: an environment variable or a .env entry wins.
Then add it to Claude Code:  claude mcp add --transport http vanna-solana http://127.0.0.1:8000/mcp
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

port = os.environ.get("PORT", "8000")
DEFAULTS = {
    "MCP_TRANSPORT": "streamable-http",
    "MCP_HTTP_HOST": "127.0.0.1",
    "PORT": port,
    "SAFETY_LAYER": "true",
    "ENABLE_SIGN_LINKS": "true",
    "ENABLE_TEST_FAUCET": "true",
    "SIGN_BASE_URL": f"http://127.0.0.1:{port}",
    "AUTH_ENABLED": "false",
    # Lets margin swaps into JupSOL / JitoSOL fit in one transaction (the staging fork's frozen table; skipped if absent).
    "PROTOCOL_LOOKUP_TABLES": "5gCxkk3VFCE96Ueej8sc6bm3wjJahBPvgw4ewaknSF74",
}
for key, value in DEFAULTS.items():
    os.environ.setdefault(key, value)

from mcp_server.main import main  # noqa: E402  (after the environment is set: settings are read at import)

if __name__ == "__main__":
    print(f"Vanna Solana MCP  ->  http://127.0.0.1:{port}/mcp   (signing pages on the same address)")
    print(f"Claude Code:  claude mcp add --transport http vanna-solana http://127.0.0.1:{port}/mcp")
    main()
