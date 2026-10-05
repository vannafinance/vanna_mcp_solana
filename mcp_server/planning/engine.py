"""The plan engine: frozen plans, approval by the user's wallet, step-by-step execution, exactly-once.

The website Copilot's workflow engine, for an MCP client. The client LLM does the thinking (what the user wants,
which strategy, how to explain it); everything that has to be right is here:

  * A plan is frozen with exact amounts, fingerprinted (sha256 over every executable slot, the wallet and the
    health floor) and stored. What executes is what was approved: the client never sends amounts back.
  * Approval is the user's own wallet signing a message that names the digest, in the plan page — not a claim the
    model makes. It must arrive within the approval window; a stale plan is re-drawn from fresh reads.
  * Each step is pre-flighted against live data when its turn comes (resolving "all", re-checking the 1.15 floor,
    the spend caps, funding) and then built and simulated; a step that would breach the floor pauses the plan
    instead of pushing on.
  * Steps run in order, and each is claimed at most once (a step is built, signed and relayed one time; an unclear
    outcome is marked `uncertain` and never retried automatically; a transaction that did not land before its
    blockhash expired is marked `expired` and rebuilt).
  * The server never signs. It checks that what the wallet signed is byte-identical to what it built, then relays
    it and watches the confirmation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable

from solders.transaction import VersionedTransaction

from mcp_server import internal, signing
from mcp_server.oauth import siws
from mcp_server.oauth.store import RecordStore
from mcp_server.planning import facts
from mcp_server.planning.guard import guard_direct_write, step_from_call
from mcp_server.planning.preflight import Preflight, humanize, preflight_step, validate_steps
from mcp_server.planning.strategies import label_for
from mcp_server.safety import Safety
from vanna_core.config import get_settings

logger = logging.getLogger(__name__)

RECORD_TTL_SECONDS = 24 * 3600
# A transaction that has not appeared this long after it was sent cannot land any more (blockhash validity is far shorter).
BLOCKHASH_DEAD_AFTER_SECONDS = 180
FINAL_STEP_STATES = frozenset({"confirmed", "skipped", "failed", "uncertain"})
FINAL_PLAN_STATES = frozenset({"completed", "failed", "cancelled", "expired"})


class PlanError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def plan_digest(wallet: str, hf_floor: float, steps: list[dict[str, Any]]) -> str:
    """sha256 over every executable slot of every step, the wallet and the health floor (not the wording)."""
    canonical = json.dumps(
        {"v": 1, "w": wallet, "f": hf_floor,
         "s": [[s.get("kind", "write"), s["op"], s.get("asset"), s.get("amount"),
                sorted((s.get("slots") or {}).items()), s.get("tool"), sorted((s.get("args") or {}).items())]
               for s in steps]},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def approval_message(rec: dict[str, Any]) -> str:
    steps = "\n".join(f"{i + 1}. {s['label']}" for i, s in enumerate(rec["steps"]))
    expires = datetime.fromtimestamp(rec["approval_expires_at"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "Vanna Finance — approve this plan\n"
        f"Wallet: {rec['wallet']}\n"
        f"Plan: {rec['id']}\n"
        f"Digest: {rec['digest']}\n"
        f"Health-factor floor: {rec['hf_floor']:.2f}\n"
        f"Steps:\n{steps}\n"
        "Signing this message approves exactly these steps. It does not move funds; each step still needs its own "
        "transaction signature from your wallet.\n"
        f"Approve before: {expires}"
    )


class PlanEngine:
    def __init__(self, store: RecordStore, safety: Safety, sender: signing.Sender,
                 status_reader: signing.StatusReader, base_url: Callable[[], str],
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.safety = safety
        self._send = sender
        self._statuses = status_reader
        self._base_url = base_url
        self._clock = clock

    # ------------------------------------------------------------------ creation

    async def create(self, wallet: str, drawn: dict[str, Any], *, kind: str) -> dict[str, Any]:
        """Freeze a drawn plan ({summary, steps, warnings, simulation, hf_floor}) for approval."""
        settings = get_settings()
        problem = validate_steps(drawn["steps"])
        if problem:
            raise PlanError("invalid_plan", problem)
        now = self._clock()
        steps = []
        for s in drawn["steps"]:
            step = {**s, "kind": s.get("kind", "write"), "slots": dict(s.get("slots") or {}), "label": label_for(s),
                    "state": "pending", "attempts": 0}
            steps.append(step)
        plan_id = secrets.token_urlsafe(32)
        rec = {
            "id": plan_id, "kind": kind, "wallet": wallet, "summary": drawn["summary"],
            "warnings": drawn.get("warnings", []), "simulation": drawn.get("simulation"),
            "hf_floor": drawn["hf_floor"], "steps": steps, "digest": plan_digest(wallet, drawn["hf_floor"], drawn["steps"]),
            "status": "awaiting_approval", "created_at": now,
            "approval_expires_at": now + settings.plan_approval_ttl_seconds, "approved_at": None, "run_until": None,
        }
        await self.store.put(f"plan:{plan_id}", rec, RECORD_TTL_SECONDS)
        return self.view(rec)

    async def create_from_call(self, wallet: str, tool: str, args: dict[str, Any], summary: str, simulated_usd: float = 0.0) -> dict[str, Any]:
        """A direct write is a one-step plan (the Copilot's rule), so it gets the same approval and confirmation path."""
        mapped = step_from_call(tool, args)
        step = mapped or {"kind": "write", "op": "raw", "asset": None, "amount": None, "slots": {},
                          "tool": tool, "args": {k: v for k, v in args.items()}, "label": summary}
        floor = get_settings().min_health_factor
        return await self.create(wallet, {"summary": summary, "steps": [step], "warnings": [], "simulation": None, "hf_floor": floor},
                                 kind="single")

    # ------------------------------------------------------------------ reading

    async def _load(self, plan_id: str) -> dict[str, Any]:
        if not isinstance(plan_id, str) or not 20 <= len(plan_id) <= 80:
            raise PlanError("not_found", "This plan link is not valid.", 404)
        rec = await self.store.get(f"plan:{plan_id}")
        if rec is None:
            raise PlanError("not_found", "This plan link is not valid or has expired.", 404)
        return rec

    async def _save(self, rec: dict[str, Any]) -> None:
        remaining = max(1.0, rec["created_at"] + RECORD_TTL_SECONDS - self._clock())
        await self.store.put(f"plan:{rec['id']}", rec, remaining)

    def plan_url(self, plan_id: str) -> str:
        return f"{self._base_url().rstrip('/')}/plan/{plan_id}"

    def view(self, rec: dict[str, Any]) -> dict[str, Any]:
        """What is safe to show anyone holding the link and what the model needs: no transactions, no secrets."""
        now = self._clock()
        return {
            "plan_id": rec["id"], "kind": rec["kind"], "wallet": rec["wallet"], "summary": rec["summary"],
            "status": rec["status"], "digest": rec["digest"], "hf_floor": rec["hf_floor"],
            "warnings": rec["warnings"], "simulation": rec.get("simulation"),
            "approve_within_seconds": max(0, int(rec["approval_expires_at"] - now)) if rec["status"] == "awaiting_approval" else 0,
            "steps": [
                {"index": i, "label": s["label"], "state": s["state"], "kind": s["kind"],
                 "amount": s.get("resolved_amount") or s.get("amount"), "asset": s.get("asset"),
                 "usd": s.get("usd"), "notes": s.get("notes", []), "signature": s.get("signature"),
                 "error": s.get("error")}
                for i, s in enumerate(rec["steps"])
            ],
            "plan_url": self.plan_url(rec["id"]),
            "approval_message": approval_message(rec) if rec["status"] == "awaiting_approval" else None,
        }

    async def get(self, plan_id: str) -> dict[str, Any]:
        rec = await self._load(plan_id)
        await self._refresh(rec)
        return self.view(rec)

    async def status_for(self, wallet: str | None, plan_id: str) -> dict[str, Any]:
        rec = await self._load(plan_id)
        if wallet is not None and rec["wallet"] != wallet:
            raise PlanError("not_found", "No such plan for your wallet.", 404)
        await self._refresh(rec)
        return self.view(rec)

    # ------------------------------------------------------------------ approval

    async def approve(self, plan_id: str, wallet: str, signature_b64: str) -> dict[str, Any]:
        rec = await self._load(plan_id)
        if rec["status"] in FINAL_PLAN_STATES:
            raise PlanError("not_open", f"This plan is {rec['status']}.", 409)
        if rec["status"] != "awaiting_approval":
            return self.view(rec)  # approving twice is harmless
        if self._clock() > rec["approval_expires_at"]:
            rec["status"] = "expired"
            await self._save(rec)
            raise PlanError("expired", "This plan was not approved in time. Ask for it again and it is redrawn from fresh data.", 410)
        if wallet != rec["wallet"]:
            raise PlanError("wrong_wallet", "Connect the wallet this plan was drawn for.", 403)
        if not siws.verify_signature(wallet, approval_message(rec), signature_b64):
            raise PlanError("invalid_signature", "The approval signature did not verify.", 401)
        rec["status"] = "approved"
        rec["approved_at"] = self._clock()
        rec["run_until"] = rec["approved_at"] + get_settings().plan_run_window_seconds
        await self._save(rec)
        logger.info("plan approved | wallet=%s plan=%s digest=%s", wallet, plan_id, rec["digest"])
        return self.view(rec)

    async def cancel(self, plan_id: str, wallet: str | None = None) -> dict[str, Any]:
        rec = await self._load(plan_id)
        if wallet is not None and rec["wallet"] != wallet:
            raise PlanError("not_found", "No such plan for your wallet.", 404)
        if rec["status"] not in FINAL_PLAN_STATES:
            rec["status"] = "cancelled"
            await self._save(rec)
        return self.view(rec)

    # ------------------------------------------------------------------ running

    def _open_step(self, rec: dict[str, Any]) -> int | None:
        for i, s in enumerate(rec["steps"]):
            if s["state"] not in ("confirmed", "skipped"):
                return i
        return None

    async def _finish_if_done(self, rec: dict[str, Any]) -> None:
        if rec["status"] in FINAL_PLAN_STATES:
            return
        if self._open_step(rec) is None:
            rec["status"] = "completed"
        elif any(s["state"] in ("failed", "uncertain") for s in rec["steps"]):
            rec["status"] = "failed"

    async def _refresh(self, rec: dict[str, Any]) -> None:
        """Move submitted steps to confirmed/failed from the chain; expire what can no longer land."""
        now = self._clock()
        changed = False
        if rec["status"] == "awaiting_approval" and now > rec["approval_expires_at"]:
            rec["status"] = "expired"
            changed = True
        pending = [(i, s) for i, s in enumerate(rec["steps"]) if s["state"] == "submitted"]
        if pending:
            try:
                statuses = await self._statuses([s["signature"] for _, s in pending])
            except Exception:  # noqa: BLE001 — a status read failing must not break the page
                logger.warning("signature status read failed", exc_info=True)
                statuses = [None] * len(pending)
            for (_, step), status in zip(pending, statuses):
                if status is not None and status.get("err") is not None:
                    step["state"], step["error"] = "failed", f"The transaction failed on-chain: {status['err']}"
                    changed = True
                elif status is not None and status.get("confirmationStatus") in ("confirmed", "finalized"):
                    step["state"] = "confirmed"
                    changed = True
                elif status is None and now - step.get("submitted_at", now) > BLOCKHASH_DEAD_AFTER_SECONDS:
                    # Never landed and can't any more: safe to rebuild.
                    step["state"], step["error"], step["signature"] = "expired", "The transaction did not land in time; it will be rebuilt.", None
                    changed = True
        if changed or rec["status"] not in FINAL_PLAN_STATES:
            before = rec["status"]
            await self._finish_if_done(rec)
            if changed or rec["status"] != before:
                await self._save(rec)

    async def build_next(self, plan_id: str) -> dict[str, Any]:
        """Pre-flight, build and simulate the plan's next step. Returns the transaction(s) for the wallet to sign."""
        rec = await self._load(plan_id)
        await self._refresh(rec)
        if rec["status"] == "completed":
            return {"done": True, "plan": self.view(rec)}  # asking again after the last step is just a status read
        if rec["status"] in FINAL_PLAN_STATES:
            raise PlanError("not_open", f"This plan is {rec['status']}.", 409)
        if rec["status"] == "awaiting_approval":
            raise PlanError("not_approved", "Approve the plan before any step is built.", 409)
        if self._clock() > (rec["run_until"] or 0):
            rec["status"] = "expired"
            await self._save(rec)
            raise PlanError("expired", "This plan's run window has passed. Ask for it again.", 410)
        idx = self._open_step(rec)
        if idx is None:
            await self._finish_if_done(rec)
            await self._save(rec)
            return {"done": True, "plan": self.view(rec)}
        step = rec["steps"][idx]
        wallet = rec["wallet"]

        if step["state"] in ("submitted", "submitting"):
            return {"waiting": True, "step": idx, "label": step["label"], "plan": self.view(rec)}
        if step["state"] in ("uncertain", "failed"):
            raise PlanError("step_unresolved", step.get("error") or "A step has an unclear outcome, so the plan stops here.", 409)

        if step["kind"] == "read":  # the closing health report runs inline once every write before it settled
            report = await facts.load_health(wallet)
            step["state"] = "confirmed"
            step["report"] = report.raw
            await self._finish_if_done(rec)
            await self._save(rec)
            return {"done": rec["status"] == "completed", "report": report.raw, "plan": self.view(rec)}

        # Claim the step: two browsers on the same plan cannot build it twice at once.
        if not await self.store.put_if_absent(f"planlock:{plan_id}:{idx}", {"at": self._clock()}, 30):
            raise PlanError("busy", "This step is already being prepared. Wait a moment.", 409)
        try:
            pre = await self._preflight(wallet, step)
            if pre.skip:
                step["state"], step["notes"] = "skipped", [pre.skip]
                await self._finish_if_done(rec)
                await self._save(rec)
                return {"skipped": True, "step": idx, "reason": pre.skip, "plan": self.view(rec)}
            if not pre.ok or pre.prepared is None:
                step["error"] = pre.message
                rec["status"] = "paused" if pre.code in ("risk", "limit") else rec["status"]
                await self._save(rec)
                return {"ok": False, "step": idx, "label": step["label"], "code": pre.code, "reason": pre.message, "plan": self.view(rec)}
            prepared = pre.prepared
            rate = await self.safety.consume_write(wallet)
            if not rate.ok:
                return {"ok": False, "step": idx, "code": "limit", "reason": rate.message, "plan": self.view(rec)}
            envelope = await internal.call_raw(prepared.tool, **prepared.args)
            if not isinstance(envelope, dict) or not envelope.get("transactions"):
                step["error"] = humanize(envelope.get("message") if isinstance(envelope, dict) else None)
                step["attempts"] += 1
                await self._save(rec)
                return {"ok": False, "step": idx, "code": envelope.get("error", "build_failed") if isinstance(envelope, dict) else "build_failed",
                        "reason": step["error"], "plan": self.view(rec)}
            if not envelope.get("simulation_success"):
                step["error"] = humanize(envelope.get("reason"))
                step["attempts"] += 1
                await self._save(rec)
                return {"ok": False, "step": idx, "code": envelope.get("code") or "simulation_failed",
                        "reason": step["error"], "plan": self.view(rec)}
            built: dict[str, str] = {}
            out_txs = []
            for entry in envelope["transactions"]:
                tx = VersionedTransaction.from_bytes(base64.b64decode(entry["unsignedTransaction"]))
                if signing.fee_payer(tx) != wallet:
                    raise PlanError("build_failed", "The prepared transaction is not for the plan's wallet.", 500)
                built[entry["id"]] = signing.message_hash(tx)
                out_txs.append({"id": entry["id"], "purpose": entry.get("purpose"), "dependsOn": entry.get("dependsOn", []),
                                "unsignedTransaction": entry["unsignedTransaction"],
                                "lastValidBlockHeight": entry.get("lastValidBlockHeight")})
            step.update({"state": "built", "built": built, "usd": prepared.usd, "resolved_amount": prepared.resolved_amount,
                         "notes": prepared.notes, "risk": prepared.risk.to_dict(), "error": None})
            if rec["status"] in ("approved", "paused"):
                rec["status"] = "running"
            await self._save(rec)
            return {"ok": True, "step": idx, "label": step["label"], "summary": envelope.get("summary"),
                    "notes": prepared.notes, "risk": prepared.risk.to_dict(), "simulation": prepared.simulation,
                    "transactions": out_txs, "plan": self.view(rec)}
        finally:
            await self.store.delete(f"planlock:{plan_id}:{idx}")

    async def _preflight(self, wallet: str, step: dict[str, Any]) -> Preflight:
        if step["op"] == "raw":
            return await guard_direct_write(wallet, step["tool"], step["args"], consume_rate=False)
        return await preflight_step(wallet, step)

    async def submit(self, plan_id: str, tx_id: str, signed_b64: str) -> dict[str, Any]:
        """Check the wallet's signed transaction is exactly what was built, relay it, and start watching it."""
        rec = await self._load(plan_id)
        if rec["status"] in FINAL_PLAN_STATES:
            raise PlanError("not_open", f"This plan is {rec['status']}.", 409)
        idx = next((i for i, s in enumerate(rec["steps"]) if s["state"] == "built"), None)
        if idx is None:
            raise PlanError("not_built", "No step is waiting for a signature. Prepare the step first.", 409)
        step = rec["steps"][idx]
        expected = (step.get("built") or {}).get(tx_id) if isinstance(tx_id, str) else None
        if expected is None:
            raise PlanError("not_built", "That transaction was not prepared by this plan.", 409)
        try:
            raw = base64.b64decode(signed_b64, validate=True)
            if len(raw) > signing.MAX_SIGNED_TX_BYTES:
                raise ValueError("too large")
            tx = VersionedTransaction.from_bytes(raw)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise PlanError("invalid_transaction", "That is not a valid signed transaction.") from exc
        if signing.message_hash(tx) != expected:
            raise PlanError("transaction_changed", "The signed transaction differs from the one prepared (the wallet may have modified it). Nothing was sent.")
        reason = signing.verify_signed_transaction(tx, rec["wallet"])
        if reason:
            raise PlanError("invalid_signature", reason, 401)
        # Exactly once: the state moves before anything is sent, so a second submit of the same step is refused.
        step["state"], step["submitted_at"] = "submitting", self._clock()
        await self._save(rec)
        try:
            signature = await self._send(base64.b64encode(raw).decode())
        except signing.SendError as exc:
            step["state"], step["error"] = "built", f"The network rejected the transaction: {str(exc)[:300]}"
            await self._save(rec)
            raise PlanError("send_failed", step["error"], 502) from exc
        except Exception as exc:  # noqa: BLE001 — outcome unclear: never retried automatically
            step["state"], step["error"] = "uncertain", "The result of sending this step is unclear, so it was not retried. Check your wallet's activity."
            rec["status"] = "failed"
            await self._save(rec)
            logger.warning("relay outcome unclear | plan=%s step=%s: %s", plan_id, idx, exc)
            raise PlanError("uncertain", step["error"], 502) from exc
        step["state"], step["signature"], step["error"] = "submitted", signature, None
        await self.safety.record_spend(rec["wallet"], float(step.get("usd") or 0.0))
        await self._save(rec)
        logger.info("signed transaction relayed | wallet=%s plan=%s step=%s signature=%s", rec["wallet"], plan_id, idx, signature)
        return {"signature": signature, "step": idx, "plan": self.view(rec)}


# ---------------------------------------------------------------------- module-level registration

_engine: PlanEngine | None = None


def set_engine(engine: PlanEngine | None) -> None:
    global _engine
    _engine = engine


def get_engine() -> PlanEngine | None:
    return _engine
