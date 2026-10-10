"""Protocol-wide reads: `ProtocolConfig`, asset discovery, `Integration`
whitelist. Backs the `vanna_protocol_info` dispatcher (TOOLS_CATALOGUE.md §3.2).

No writes here — admin instructions are deliberately never wrapped
(TOOLS_CATALOGUE.md §4/§6.1 item 7, "No admin wrappers in v1").
"""

from __future__ import annotations

import asyncio

from vanna_core.assets import JUPITER_V6_PROGRAM_ID, KLEND_PROGRAM_ID, LEND_BORROW_SYMBOLS, AssetRegistry
from vanna_core.client import SolanaClient
from vanna_core.layouts import Integration as IntegrationLayout
from vanna_core.layouts import ProtocolConfig as ProtocolConfigLayout
from vanna_core.layouts import Reserve as ReserveLayout
from vanna_core.layouts import pubkey_bytes_to_base58
from vanna_core.pdas import integration_pda, protocol_config_pda
from solders.pubkey import Pubkey

# Operating modes — TOOLS_CATALOGUE.md §2.5, verified against `validation/accounts.rs`.
_OPERATING_MODES: dict[int, tuple[str, str]] = {
    0: ("Normal", "No restrictions"),
    1: ("BorrowPaused", "Blocks borrow; deposit/withdraw/repay/liquidate/external calls still allowed"),
    2: ("WithdrawOnly", "Allows only repay, redeem, collateral deposit/withdraw, liquidate, external calls"),
    3: ("Halted", "Allows only repay and collateral deposit"),
}

_RESERVE_STATUSES: dict[int, tuple[str, str]] = {
    0: ("Active", "No restrictions"),
    1: ("SupplyOnly", "Blocks borrow"),
    2: ("RepayOnly", "Blocks supply and borrow"),
    3: ("Frozen", "Blocks supply, redeem, and borrow"),
}

# The all-zero pubkey (base58 of 32 zero bytes) — `AssetConfig.reserve` holds
# this for a collateral-only asset (e.g. a Kamino receipt) that never had
# `admin_initialize_reserve` called for it.
_ZERO_PUBKEY = "11111111111111111111111111111111"

_INTEGRATION_PROGRAMS: tuple[tuple[str, "object"], ...] = (
    ("KaminoLend", KLEND_PROGRAM_ID),
    ("Jupiter", JUPITER_V6_PROGRAM_ID),
)


async def _none() -> None:
    return None


class ProtocolContract:
    def __init__(self, client: SolanaClient) -> None:
        self._client = client

    async def get_config(self) -> dict:
        address, _ = protocol_config_pda(self._client.program_id)
        cfg = await self._client.fetch_program_account(address, "ProtocolConfig", ProtocolConfigLayout)
        mode_name, mode_blocks = _OPERATING_MODES.get(cfg.operating_mode, ("Unknown", "Unknown mode"))
        return {
            "admin": pubkey_bytes_to_base58(cfg.admin),
            "pending_admin": pubkey_bytes_to_base58(cfg.pending_admin) if any(cfg.pending_admin) else None,
            "treasury": pubkey_bytes_to_base58(cfg.treasury),
            "operating_mode": mode_name,
            "operating_mode_code": cfg.operating_mode,
            "operating_mode_blocks": mode_blocks,
            "max_assets_per_margin": cfg.max_assets_per_margin,
            "max_debts_per_margin": cfg.max_debts_per_margin,
            "max_venues_per_margin": cfg.max_venues_per_margin,
            # Token collateral is protocol-wide since 2026-10-09: every margin is scanned over these asset indexes.
            "allowed_token_indexes": [int(i) for i in cfg.allowed_tokens if int(i) != 65_535],
            "next_asset_index": cfg.next_asset_index,
        }

    async def list_assets(self) -> list[dict]:
        registry = AssetRegistry(self._client)
        await registry.load()
        discovered = registry.all()  # every AssetConfig the program owns, product assets first

        # Kamino receipts are collateral-only and have no Vanna Reserve (no
        # Earn pool for them) — their AssetConfig.reserve field is the
        # all-zero placeholder, never a real account to fetch.
        fetchable = [d.reserve != _ZERO_PUBKEY for d in discovered]
        reserves_or_none = await asyncio.gather(
            *(
                self._client.fetch_program_account_or_none(Pubkey.from_string(d.reserve), "Reserve", ReserveLayout)
                if can_fetch
                else _none()
                for d, can_fetch in zip(discovered, fetchable, strict=True)
            )
        )

        out: list[dict] = []
        for d, reserve in zip(discovered, reserves_or_none, strict=True):
            status_name = None
            if reserve is not None:
                status_name, _ = _RESERVE_STATUSES.get(reserve.status, ("Unknown", "Unknown status"))
            out.append(
                {
                    "symbol": d.symbol,
                    "mint": d.mint,
                    "asset_index": d.asset_index,
                    "decimals": d.decimals,
                    "token_program": d.token_program,
                    "reserve": d.reserve if reserve is not None else None,
                    "reserve_status": status_name,
                    "price_source": d.price_source,
                    "oracle_program": d.oracle_program,
                    "oracle": {
                        "scope": d.oracle.uses_scope,
                        "scope_chain": [e for e in d.oracle.scope_chain if e != 0xFFFF],
                        "pyth_fallback": d.oracle.uses_pyth,
                        "pyth_factor": d.oracle.uses_pyth_factor,
                        "kamino_reserve": d.oracle.klend_reserve or None,
                        "max_age_secs": d.oracle.max_age_secs,
                        "max_twap_divergence_bps": d.oracle.max_twap_divergence_bps,
                        "max_confidence_bps": d.oracle.max_confidence_bps,
                    },
                    "lend_borrow_offered": d.symbol.upper() in LEND_BORROW_SYMBOLS,
                    "collateral_enabled": d.collateral_enabled,
                    "borrow_enabled": d.borrow_enabled,
                    "max_collateral_per_margin": d.max_collateral_per_margin,
                }
            )
        return out

    async def list_integrations(self) -> list[dict]:
        async def fetch_one(name: str, program_id) -> dict | None:
            address, _ = integration_pda(program_id, self._client.program_id)
            record = await self._client.fetch_program_account_or_none(address, "Integration", IntegrationLayout)
            if record is None:
                return None
            return {
                "adapter": name,
                "program_id": pubkey_bytes_to_base58(record.program_id),
                # The validator agent that reviews every margin_execute call into this program.
                "validator": pubkey_bytes_to_base58(record.validator),
                "enabled": record.enabled,
            }

        results = await asyncio.gather(*(fetch_one(name, prog) for name, prog in _INTEGRATION_PROGRAMS))
        return [r for r in results if r is not None]
