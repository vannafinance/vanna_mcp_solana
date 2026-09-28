# Vanna MCP — Solana

MCP server for the Vanna Finance lending protocol on Solana, deployed at
`BZ812nUv4Qhr2p1JVgmoJGjYTGk1brAXckyhFSCNH3Zg` (devnet). Sibling to the
Stellar/Soroban `vanna-mcp` server, adapted for a single-program, no-registry
protocol — see `VANNA_SOLANA_MCP_REQUIREMENTS.md` in the `Backend-Solana` repo
for the full contract inventory and open questions this still depends on.

## Why this isn't built on anchorpy

anchorpy 0.21.0's IDL parser cannot parse `idl/vanna_lending.json` — it uses
Anchor's newer explicit per-instruction/per-account `discriminator` array
format, and anchorpy-core (Rust) rejects it with
`SerdeJSONError: data did not match any variant of untagged enum
IdlAccountItem`. No anchorpy release compatible with this IDL was available
to install (checked 2026-09-28).

Instead, `vanna_core/anchor_codec.py` recomputes Anchor's discriminators
directly (`sha256("global:<ix>")[:8]` / `sha256("account:<Struct>")[:8]`) —
cross-checked byte-for-byte against every discriminator the IDL itself
declares — and `vanna_core/layouts.py` hand-transcribes the account structs as
`borsh_construct` layouts. This has been verified against the **live deployed
program on devnet**, not just unit-tested: `tests/test_lender_tools.py` reads
real `Reserve` accounts and simulates real instructions against it.

## Structure

```
vanna_core/            # protocol logic — no MCP/AI concerns
  config.py             # env-driven settings (program ID, RPC URL, asset table)
  pdas.py                # PDA derivation (ported from Backend-Solana's lib/solana/pdas.ts)
  layouts.py             # borsh account struct layouts
  anchor_codec.py         # discriminator derivation (see above)
  math_utils.py           # decimal conversions, health-factor formula, rate curve
  client.py               # async RPC + instruction-build/simulate wrapper
  contracts/
    reserve.py             # Earn: lender_supply, lender_redeem, pool stats
    # protocol_config.py, asset_config.py, margin_account.py, debt_position.py,
    # collateral.py, repay.py, liquidation.py, oracle.py — NOT YET BUILT,
    # see VANNA_SOLANA_MCP_REQUIREMENTS.md Part B for their spec.

mcp_server/             # MCP transport + tool registration
  dependencies.py         # VannaContext DI container
  error_handling.py       # @tool_safe decorator
  main.py                 # entrypoint (mcp>=2.2.0 MCPServer, stdio/streamable-http)
  tools/
    lender_tools.py        # vanna_get_pool_stats, vanna_supply_liquidity, ...
    # remaining tool groups follow the same pattern once their contracts exist

idl/vanna_lending.json  # copied from Backend-Solana/idl/ — keep in sync manually
                        # until Ask #1 in VANNA_SOLANA_MCP_REQUIREMENTS.md is answered
```

## What's implemented vs. what's next

Implemented and tested against live devnet: **Earn only** (`ReserveContract` +
`lender_tools`) — pool stats, deposited balance, supply/redeem/refresh
instruction building + simulation.

Everything else in `VANNA_SOLANA_MCP_REQUIREMENTS.md` Part B (margin account
lifecycle, collateral, borrow, repay, liquidation, Pyth price refresh) is
speced there but not yet built. They follow the exact same pattern as
`reserve.py` + `lender_tools.py`: PDA derivation (`pdas.py`), a borsh layout
(`layouts.py`), instruction discriminator + account list from the IDL,
`client.build_instruction` + `client.simulate_and_build_unsigned`. The borrow/
withdraw/liquidate paths are more involved because they need a fresh Pyth
price posted first and a variable `remaining_accounts` list built from the
margin account's other open positions (see `lib/margin-utils.ts`
`remainingPositionAccounts` in Backend-Solana) — worth their own pass rather
than rushing.

`vanna_supply_liquidity`/`vanna_redeem_liquidity` also don't yet prepend the
ATA-create / SOL-wrap prefix instructions Backend-Solana's frontend adds
before every deposit (`ensureTokenAccountInstructions`,
`createSyncNativeInstruction`) — noted in `reserve.py`'s docstring.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env
pytest tests/ -v          # hits live devnet — no mocks
python -m mcp_server.main # stdio transport by default
```

## Keeping the IDL in sync with Backend-Solana

`idl/vanna_lending.json` here is a manual copy of `Backend-Solana/idl/vanna_lending.json`.
Re-copy it whenever the deployed program changes. See
`VANNA_SOLANA_MCP_REQUIREMENTS.md` Part A #1 for the open question of which
program ID / IDL pair is actually the target deployment for this MCP.
