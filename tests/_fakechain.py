"""An in-memory stand-in for the MCP's own tools, for offline tests of sizing, pre-flight, plans and signing.

`install(monkeypatch, chain)` replaces `mcp_server.internal.call_raw`, so every `internal.call(...)` the planner makes
is answered from `FakeChain` state with the same shapes the real dispatchers return (keys verified against
vanna_core/contracts). Write tools return a real unsigned v0 transaction whose fee payer is the wallet, so the
signing checks (message hash, ed25519 signature) run for real.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from mcp_server import internal
from mcp_server.internal import ToolError

MEMO = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
PRICES = {"SOL": 100.0, "USDC": 1.0, "USDT": 1.0, "JitoSOL": 110.0, "JupSOL": 108.0}
DECIMALS = {"SOL": 9, "USDC": 6, "USDT": 6, "JitoSOL": 9, "JupSOL": 9}


def unsigned_tx_b64(wallet: str, tag: str = "x") -> str:
    ix = Instruction(MEMO, tag.encode(), [AccountMeta(Pubkey.from_string(wallet), True, False)])
    message = MessageV0.try_compile(Pubkey.from_string(wallet), [ix], [], Hash.default())
    tx = VersionedTransaction.populate(message, [Signature.default()])
    return base64.b64encode(bytes(tx)).decode()


def sign_b64(unsigned_b64: str, keypair: Keypair) -> str:
    tx = VersionedTransaction.from_bytes(base64.b64decode(unsigned_b64))
    return base64.b64encode(bytes(VersionedTransaction(tx.message, [keypair]))).decode()


def sign_wrong_key(unsigned_b64: str, keypair: Keypair) -> str:
    """A transaction carrying a signature from a key that is not its fee payer (what a hostile page could send)."""
    from solders.message import to_bytes_versioned

    tx = VersionedTransaction.from_bytes(base64.b64decode(unsigned_b64))
    forged = VersionedTransaction.populate(tx.message, [keypair.sign_message(to_bytes_versioned(tx.message))])
    return base64.b64encode(bytes(forged)).decode()


def _fmt(x: float, asset: str) -> str:
    text = f"{x:.{DECIMALS[asset]}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass
class FakeChain:
    wallet: str
    has_account: bool = True
    collateral: dict[str, float] = field(default_factory=dict)  # asset -> units in the margin vault
    debt: dict[str, float] = field(default_factory=dict)
    wallet_tokens: dict[str, float] = field(default_factory=lambda: {"USDC": 1000.0, "USDT": 0.0})
    sol_balance: float = 10.0
    earn_shares: dict[str, float] = field(default_factory=dict)  # asset -> shares (1:1 with underlying here)
    farm: dict[str, float] = field(default_factory=dict)  # underlying -> amount supplied (receipt 1:1)
    pool_liquidity: dict[str, float] = field(default_factory=lambda: {"SOL": 1e6, "USDC": 1e6, "USDT": 1e6})
    prices: dict[str, float] = field(default_factory=lambda: dict(PRICES))
    slippage_loss: float = 0.001  # the quote's fee/impact
    simulation_ok: bool = True
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # ---------------------------------------------------------------- derived
    def _usd(self, rows: dict[str, float]) -> float:
        return sum(u * self.prices[a] for a, u in rows.items())

    @property
    def c(self) -> float:
        return self._usd(self.collateral)

    @property
    def d(self) -> float:
        return self._usd(self.debt)

    def _hf(self, c: float, d: float) -> str | None:
        return None if d <= 0 else str(c / d)

    def health(self) -> dict[str, Any]:
        if not self.has_account:
            raise ToolError("account_not_found", "no margin account")
        row = lambda a, u, k: {"symbol": a, k: _fmt(u, a), "usd_value": f"{u * self.prices[a]:.2f}", "price": {"usd": str(self.prices[a])}}
        return {
            "margin_account": "MarginPDA1111111111111111111111111111111111",
            "collateral": [row(a, u, "ui_balance") for a, u in self.collateral.items() if u > 0],
            "debt": [row(a, u, "ui_debt") for a, u in self.debt.items() if u > 0],
            "total_collateral_usd": f"{self.c:.2f}", "total_debt_usd": f"{self.d:.2f}",
            "health_factor": self._hf(self.c, self.d), "is_healthy": True,
            "is_liquidatable": self.d > 0 and self.c / self.d <= 1.10, "price_checks_pass": True,
        }

    # ---------------------------------------------------------------- tool dispatch
    async def call_raw(self, tool: str, **a: Any) -> dict[str, Any]:
        self.calls.append((tool, a))
        action = a.get("action")
        if tool == "vanna_account":
            if action == "health":
                return self.health()
            if action == "resolve":
                return {"exists": self.has_account, "authority": a["authority"]}
            if action in ("create", "close"):
                return self._envelope(a["authority"], f"account_{action}")
        if tool == "vanna_oracle":
            if action == "price":
                return {"symbol": a["asset"], "price_usd": str(self.prices[a["asset"]]), "stale": False}
            if action == "prices":
                return {s: {"price_usd": str(self.prices[s]), "stale": False} for s in a["assets"]}
        if tool == "vanna_margin_status":
            return self._margin_status(action, a)
        if tool == "vanna_wallet" and action == "summary":
            return {"authority": a["authority"], "sol_balance": str(self.sol_balance), "margin_account_exists": self.has_account,
                    "assets": [{"symbol": s, "wallet_balance": str(v), "lender_share_balance": None,
                                "lender_redeemable_underlying": None} for s, v in self.wallet_tokens.items()]}
        if tool == "vanna_earn_market" and action == "pool_stats":
            return {"asset_symbol": a["asset"], "available_liquidity": str(self.pool_liquidity[a["asset"]]),
                    "supply_apr_pct": "3.2", "borrow_apr_pct": "5.1", "utilization_rate_pct": "40"}
        if tool == "vanna_earn_position":
            shares = self.earn_shares.get(a["asset"], 0.0)
            return {"share_balance": str(shares), "redeemable_underlying": str(shares), "earned_interest": "0.5", "complete": True}
        if tool == "vanna_farm_overview" and action == "positions":
            if not self.has_account:
                raise ToolError("account_not_found", "no margin account")
            return {"positions": [{"symbol": f"k{u}", "receipt_balance": str(v), "underlying_amount": str(v)} for u, v in self.farm.items() if v > 0]}
        if tool == "vanna_swap":
            return self._swap(action, a)
        if tool in ("vanna_earn_write", "vanna_margin_trade", "vanna_farm_kamino"):
            return self._envelope(a.get("owner") or a.get("payer") or a["authority"], f"{tool}:{action}")
        raise AssertionError(f"FakeChain has no handler for {tool} {a}")

    def _margin_status(self, action: str, a: dict[str, Any]) -> dict[str, Any]:
        asset, amount = a.get("asset"), float(a.get("amount") or 0)
        usd = amount * self.prices.get(asset, 0)
        if action == "can_borrow":
            c, d = self.c + usd, self.d + usd
            hf = c / d
            ok = hf > 1.10
            return {"allowed": ok, "reason": None if ok else "would breach the 1.10 health-factor threshold",
                    "current_health_factor": self._hf(self.c, self.d), "projected_health_factor": str(hf)}
        if action == "can_withdraw":
            row = self.collateral.get(asset, 0.0)
            if amount > row + 1e-12:
                return {"allowed": False, "reason": "amount exceeds vault balance"}
            c, d = self.c - usd, self.d
            hf = None if d <= 0 else c / d
            ok = d <= 0 or hf > 1.10
            return {"allowed": ok, "reason": None if ok else "would breach the 1.10 health-factor threshold",
                    "current_health_factor": self._hf(self.c, self.d), "projected_health_factor": None if hf is None else str(hf)}
        if action == "max_borrow":
            top = max(0.0, (self.c - 1.10 * self.d) / 0.10)
            return {"asset": asset, "max_amount": _fmt(top / self.prices[asset], asset)}
        if action == "liquidation_snapshot":
            hf = None if self.d <= 0 else self.c / self.d
            return {"health_factor": None if hf is None else str(hf), "health_at_or_below_threshold": hf is not None and hf <= 1.10,
                    "total_debt_usd": f"{self.d:.2f}", "total_collateral_usd": f"{self.c:.2f}"}
        raise AssertionError(f"FakeChain margin_status {action}")

    def _swap(self, action: str, a: dict[str, Any]) -> dict[str, Any]:
        src, dst, amount = a["from_asset"], a["to_asset"], float(a["amount"])
        slip = int(a.get("slippage_bps", 100)) / 10_000
        out = amount * self.prices[src] / self.prices[dst] * (1 - self.slippage_loss)
        if action == "quote":
            return {"in_amount": _fmt(amount, src), "out_amount": _fmt(out, dst), "min_out_amount": _fmt(out * (1 - slip), dst),
                    "slippage_bps": a.get("slippage_bps", 100), "price_impact_pct": "0.1", "route": ["Fake"]}
        if action == "can_swap":
            min_usd = out * (1 - slip) * self.prices[dst]
            c = self.c - amount * self.prices[src] + min_usd
            hf = None if self.d <= 0 else c / self.d
            ok = self.d <= 0 or hf > 1.10
            return {"allowed": ok, "reason": None if ok else "would breach the 1.10 health-factor threshold",
                    "projected_health_factor": None if hf is None else str(hf)}
        return self._envelope(a["authority"], f"swap:{action}")

    def _envelope(self, wallet: str, tag: str) -> dict[str, Any]:
        sim_ok = self.simulation_ok
        return {
            "schemaVersion": "1", "cluster": "fake", "programId": "Prog", "feePayer": wallet,
            "transactions": [{"id": "action", "purpose": tag, "dependsOn": [], "format": "solana-v0", "encoding": "base64",
                              "unsignedTransaction": unsigned_tx_b64(wallet, tag), "blockhash": "11111111111111111111111111111111",
                              "lastValidBlockHeight": "999", "requiredSigners": [wallet], "addressLookupTables": [],
                              "simulation": {"status": "success" if sim_ok else "failed"}}],
            "summary": {"action": tag}, "signing_status": "needs_wallet_sign", "simulation_success": sim_ok,
            "code": None if sim_ok else "simulation_failed", "reason": None if sim_ok else "custom program error: 0x1771",
        }


def install(monkeypatch: Any, chain: FakeChain) -> FakeChain:
    monkeypatch.setattr(internal, "call_raw", chain.call_raw)
    # the stale-price refresh would otherwise try the network
    from mcp_server.planning import facts

    monkeypatch.setattr(facts, "_last_refresh", 1e18)
    return chain
