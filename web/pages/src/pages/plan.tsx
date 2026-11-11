/**
 * `/plan/<id>`: review and sign a plan an MCP client drew. The server holds the plan; this page only shows it, asks the
 * plan's own wallet to approve and sign, and relays what the wallet returns. It never composes a transaction or an amount.
 * Laid out as the Stellar MCP's review page: who asked, the execution card (one row per step, Approve on the step that waits
 * for the wallet), what changes on the account, and the close-tab card once it is over.
 *
 * Flow (mcp_server/planning/engine.py): the wallet signs the plan's approval message (one signature for the whole plan),
 * which returns a run token to this tab; then each step is prepared by the server (`/next`), signed by the wallet and
 * submitted (`/submit`), and confirmed before the next. Another browser re-confirms with the wallet (`resume_message`).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { PageShell } from "../components/page-shell";
import { ExecutionStepper, type StepperStep } from "../components/execution-stepper";
import { DoneNote, ReviewHeader, TransactionDetails, type DetailRow } from "../components/transaction-details";
import { DoneCard, ReviewOutcome } from "../components/done-card";
import { PlanSignIn, WalletBadge } from "../components/plan-signin";
import { identifyAgent } from "../lib/agents";
import { emailSigner, takeEmailIntent, useEmailState } from "../lib/email";
import { LOGOS } from "../lib/logos";
import { pageConfig } from "../lib/runtime";
import { connectExtension, connectSilently, isMobile, mobileLinks, onWalletsChange, walletList, type Signer } from "../lib/wallets";
import { SiteWarning } from "./site-warning";

type PlanStatus = "awaiting_approval" | "approved" | "running" | "paused" | "completed" | "failed" | "cancelled" | "expired";
type StepState = "pending" | "built" | "submitting" | "submitted" | "confirmed" | "failed" | "uncertain" | "skipped" | "expired";
interface PlanStep { index: number; label: string; state: StepState; signature?: string | null; error?: string | null; notes?: string[] }
interface Simulation { hf_before?: number | null; hf_after?: number | null; collateral_before?: number; collateral_after?: number; debt_before?: number; debt_after?: number }
interface PlanView {
  plan_id: string; wallet: string; summary: string; status: PlanStatus; warnings: string[]; simulation: Simulation | null;
  approve_within_seconds: number; steps: PlanStep[]; approval_message: string | null; resume_message: string | null;
  client_name?: string | null; client_host?: string | null;
  /** The wallet the agent signed in with: "dynamic" (email / social) or an extension's name ("Backpack" …). */
  provider?: string | null;
}
interface NextResult {
  plan?: PlanView; done?: boolean; skipped?: boolean; waiting?: boolean; ok?: boolean; reason?: string; step: number;
  batch?: boolean; transactions?: { id: string; unsignedTransaction: string }[]; risk?: { reasons?: string[] };
}

const FINAL: PlanStatus[] = ["completed", "failed", "cancelled", "expired"];
const planId = decodeURIComponent(location.pathname.split("/").pop() ?? "");
const api = `/api/plan/${encodeURIComponent(planId)}`;
const tokenKey = `vanna-plan-token:${planId}`;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const usd = (x: number) => `$${x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const errorText = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/** What the plan changes on the margin account, as the Stellar review's "What changes" panel shows it. */
function details(sim: Simulation | null): { rows: DetailRow[]; health: { before: number | null; after: number } | null } {
  if (!sim) return { rows: [], health: null };
  const rows: DetailRow[] = [];
  const num = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);
  if (num(sim.collateral_before) && num(sim.collateral_after) && Math.abs(sim.collateral_after - sim.collateral_before) > 0.005) {
    rows.push({ label: "Margin collateral", before: usd(sim.collateral_before), after: usd(sim.collateral_after),
      tone: sim.collateral_after > sim.collateral_before ? "better" : "worse" });
  }
  if (num(sim.debt_before) && num(sim.debt_after) && Math.abs(sim.debt_after - sim.debt_before) > 0.005) {
    rows.push({ label: "Outstanding debt", before: usd(sim.debt_before), after: usd(sim.debt_after),
      tone: sim.debt_after > sim.debt_before ? "worse" : "better" });
  }
  const health = num(sim.hf_after) ? { before: num(sim.hf_before) ? sim.hf_before : null, after: sim.hf_after } : null;
  return { rows, health };
}

export function PlanPage() {
  const email = useEmailState();
  const [view, setView] = useState<PlanView | null>(null);
  const [loadError, setLoadError] = useState("");
  const [signer, setSigner] = useState<Signer | null>(null);
  const [running, setRunning] = useState(false);
  /** The step being worked on now and what is happening to it. */
  const [phase, setPhase] = useState<{ step: number; status: "claiming" | "signing" | "submitting"; text?: string } | null>(null);
  const [failure, setFailure] = useState<{ step: number; text: string } | null>(null);
  const [connectError, setConnectError] = useState("");
  const [now, setNow] = useState(Date.now());
  const [, setTick] = useState(0);
  const runToken = useRef<string | null>(null);
  const deadline = useRef(0);
  const resumeEmail = useRef(false);
  const busy = useRef(false);

  useEffect(() => { try { runToken.current = sessionStorage.getItem(tokenKey); } catch { /* storage blocked: re-confirm with the wallet */ } }, []);
  const keepToken = (t: string | null | undefined) => {
    runToken.current = t || null;
    try { if (t) sessionStorage.setItem(tokenKey, t); } catch { /* kept in memory */ }
  };

  const call = useCallback(async <T,>(method: string, path: string, body?: unknown): Promise<T> => {
    const headers: Record<string, string> = body ? { "Content-Type": "application/json" } : {};
    if (runToken.current) headers["X-Plan-Token"] = runToken.current;
    const r = await fetch(api + path, { method, headers, body: body ? JSON.stringify(body) : undefined, cache: "no-store" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.message || `Request failed (${r.status})`);
    return data as T;
  }, []);

  const show = useCallback((v: PlanView) => {
    setView(v);
    deadline.current = v.status === "awaiting_approval" ? Date.now() + (v.approve_within_seconds || 0) * 1000 : 0;
  }, []);
  const refresh = useCallback(async () => show(await call<PlanView>("GET", "")), [call, show]);

  useEffect(() => {
    document.title = view?.status === "completed" ? "Done · Vanna" : "Review · Vanna";
  }, [view?.status]);
  useEffect(() => onWalletsChange(() => setTick((t) => t + 1)), []);
  useEffect(() => {
    resumeEmail.current = takeEmailIntent();
    refresh().catch((e) => setLoadError(errorText(e, "This plan could not be loaded.")));
    const clock = setInterval(() => setNow(Date.now()), 1000);
    const poll = setInterval(() => {
      if (!busy.current) refresh().catch(() => undefined);
    }, 5000);
    return () => { clearInterval(clock); clearInterval(poll); };
  }, [refresh]);

  // The email wallet of the plan's own address signs as soon as its session is there (restored, or just signed in here).
  useEffect(() => {
    if (!view || signer || !email.loggedIn || !email.address) return;
    if (email.address === view.wallet) setSigner(emailSigner(email.address));
  }, [email, view, signer]);

  // An extension wallet the sign-in used (or any this site is already allowed on) connects by itself when it is on the
  // plan's address, without a popup: the page opens with Approve ready, as the Stellar review page does. Tried when the
  // plan loads and again as wallets announce themselves; a click on a tile is still there when it is not possible.
  const trying = useRef(false);
  const tries = useRef(0);
  const provider = view?.provider ?? null;
  const planWallet = view?.wallet ?? null;
  const planOpen = !!view && !FINAL.includes(view.status);
  useEffect(() => {
    if (!planWallet || !planOpen || signer || provider === "dynamic" || trying.current || tries.current >= 3) return;
    trying.current = true; tries.current += 1;
    connectSilently(planWallet, provider)
      .then((w) => { if (w) setSigner((s) => s ?? w); })
      .finally(() => { trying.current = false; });
  });

  const open = !!view && !FINAL.includes(view.status);
  const activeIndex = view ? view.steps.findIndex((s) => !["confirmed", "skipped"].includes(s.state)) : -1;

  async function waitConfirmed(stepIndex: number): Promise<StepState> {
    for (let i = 0; i < 90; i++) {
      const v = await call<PlanView>("GET", ""); show(v);
      const s = v.steps[stepIndex].state;
      if (["confirmed", "failed", "expired", "uncertain"].includes(s)) return s;
      await sleep(2000);
    }
    return "submitted";
  }

  async function runSteps(w: Signer, current: PlanView) {
    // An approved plan opened in another browser (or after the token was lost): the wallet re-confirms it here.
    if (!runToken.current && current.resume_message) {
      setPhase({ step: Math.max(0, activeIndex), status: "signing", text: "Confirm in your wallet to continue here…" });
      const signature = await w.signMessage(current.resume_message);
      const r = await call<PlanView & { run_token: string }>("POST", "/approve", { wallet: w.address, signature });
      keepToken(r.run_token); show(r);
    }
    for (;;) {
      const r = await call<NextResult>("POST", "/next");
      if (r.plan) show(r.plan);
      if (r.done) return;
      if (r.skipped) continue;
      if (r.waiting) { setPhase({ step: r.step ?? 0, status: "claiming", text: "Waiting for the previous transaction to reach the network…" }); await sleep(2000); continue; }
      if (r.ok === false) { setFailure({ step: r.step ?? Math.max(0, activeIndex), text: `${r.reason || "This step could not be prepared."} Nothing was sent.` }); return; }
      if (!r.transactions) return;
      const idx = r.step;
      setPhase({ step: idx, status: "signing" });
      const signed = await w.signTransactions(r.transactions.map((t) => t.unsignedTransaction));
      setPhase({ step: idx, status: "submitting" });
      // A batch (a Predict trade) was signed in one approval; the server sends them in order, each confirmed before the next.
      for (let j = 0; j < r.transactions.length; j++) {
        const sent = await call<{ plan?: PlanView }>("POST", "/submit", { tx_id: r.transactions[j].id, signed: signed[j] });
        if (sent.plan) show(sent.plan);
      }
      const outcome = await waitConfirmed(idx);
      if (outcome !== "confirmed") {
        setFailure({ step: idx, text: outcome === "expired" ? "The transaction did not land in time. Retry to rebuild it." : "The step did not confirm." });
        return;
      }
    }
  }

  async function approve() {
    if (busy.current || !view || !signer) return;
    if (signer.address !== view.wallet) { setConnectError(`This plan is for ${view.wallet}, but the connected wallet is ${signer.address}.`); setSigner(null); return; }
    busy.current = true; setRunning(true); setFailure(null);
    const start = Math.max(0, activeIndex);
    try {
      let current = view;
      if (current.status === "awaiting_approval" && current.approval_message) {
        setPhase({ step: start, status: "signing" });
        const signature = await signer.signMessage(current.approval_message);
        const r = await call<PlanView & { run_token: string }>("POST", "/approve", { wallet: signer.address, signature });
        keepToken(r.run_token); show(r); current = r;
      }
      setPhase({ step: start, status: "claiming" });
      await runSteps(signer, current);
    } catch (e) {
      setFailure({ step: phase?.step ?? start, text: errorText(e, "Something went wrong. Nothing more was sent.") });
    } finally {
      busy.current = false; setRunning(false); setPhase(null);
      refresh().catch(() => undefined);
    }
  }

  // Back from a Google / X / GitHub sign-in started here: carry on once the email wallet is connected.
  useEffect(() => {
    if (resumeEmail.current && signer?.kind === "email" && view && open && !running) { resumeEmail.current = false; void approve(); }
  });

  async function cancel() {
    if (busy.current) return;
    busy.current = true;
    try { show(await call<PlanView>("POST", "/cancel", {})); setFailure(null); }
    catch (e) { setFailure({ step: Math.max(0, activeIndex), text: errorText(e, "Could not cancel.") }); }
    finally { busy.current = false; }
  }

  async function connect(id: string) {
    const entry = walletList().find((w) => w.id === id);
    if (entry && !entry.installed && isMobile()) {
      const link = mobileLinks().find((l) => l.label === entry.label);
      if (link) { window.location.href = link.href; return; }
    }
    setConnectError("");
    try {
      const w = await connectExtension(id);
      if (view && w.address !== view.wallet) {
        setConnectError(`This plan is for ${view.wallet.slice(0, 4)}…${view.wallet.slice(-4)}, but ${w.label} is on ${w.address.slice(0, 4)}…${w.address.slice(-4)}. Switch accounts in ${w.label} and try again.`);
        return;
      }
      setSigner(w);
    } catch (e) { setConnectError(errorText(e, "The wallet could not be connected.")); }
  }

  if (!view) {
    return (
      <PageShell review network={pageConfig().test ? "testnet" : "mainnet"}>
        <SiteWarning />
        <div className="rounded-2xl border border-vgray-100 bg-surface px-5 py-8 text-center text-[13px] text-vgray-500">
          {loadError || "Loading the review…"}
        </div>
      </PageShell>
    );
  }

  const agentName = view.client_name || "Your agent";
  const identity = identifyAgent(view.client_name, view.client_host);
  const agent = identity?.label ?? agentName;
  const completed = view.status === "completed";
  const cancelled = view.status === "cancelled";
  const network = pageConfig().test ? "testnet" : "mainnet";

  const steps: StepperStep[] = view.steps.map((s, i) => {
    let status: StepperStep["status"] = "pending";
    let error: string | undefined;
    if (s.state === "confirmed" || s.state === "skipped") status = "settled";
    else if (s.state === "failed" || s.state === "expired") { status = "failed"; error = s.error ?? (s.state === "expired" ? "The transaction did not land in time." : undefined); }
    else if (s.state === "uncertain") status = "uncertain";
    else if (s.state === "submitting" || s.state === "submitted") status = "submitting";
    if (phase && phase.step === i && status === "pending") status = phase.status;
    if (failure && failure.step === i && status !== "settled") { status = "failed"; error = failure.text; }
    // The step that waits for the wallet: Approve sits on its row (with Cancel beside it), as on the Stellar review.
    // Before a wallet is connected the row still says it waits for a signature; Approve appears once one is connected.
    if (!running && open && i === activeIndex && status === "pending") status = "signing";
    return {
      id: String(i), label: s.label, note: s.state === "skipped" ? "Skipped: already done" : undefined, op: "", asset: "", amount: "",
      status, txHash: s.signature ?? undefined, error,
      liveText: phase && phase.step === i ? phase.text : undefined,
    };
  });

  const { rows, health } = details(view.simulation);
  const left = deadline.current ? Math.max(0, Math.round((deadline.current - now) / 1000)) : 0;
  const mobile = isMobile();
  const wallets = walletList().map((w) => ({ ...w, live: w.installed || mobile }));
  // The wallet the plan is for, named the way the sign-in was made (as the Stellar review's wallet badge).
  const kind = signer ? (signer.kind === "email" ? "dynamic" : signer.label) : view.provider ?? null;
  const badge = kind === "dynamic"
    ? { label: "Email wallet", logo: LOGOS.dynamic }
    : kind
      ? { label: kind, logo: walletList().find((w) => w.label.toLowerCase() === kind.toLowerCase())?.logo ?? LOGOS.dynamic }
      : { label: "Wallet", logo: LOGOS.dynamic };
  const hasDetails = rows.length > 0 || !!health;

  return (
    <PageShell review network={network}>
      <SiteWarning />
      <ReviewHeader agent={agent} identity={identity} steps={steps.length} done={completed} cancelled={cancelled}
        aside={<WalletBadge label={badge.label} logo={badge.logo} address={view.wallet} />} />
      <ReviewOutcome
        over={completed || cancelled}
        done={<DoneCard agent={agent} steps={steps} network={network} outcome={cancelled ? "cancelled" : "done"} />}
      >
        <div className={`grid items-stretch gap-4 ${hasDetails ? "md:grid-cols-[minmax(0,1fr)_400px]" : "mx-auto w-full max-w-[720px]"}`}>
          {/* Stellar's review fills the column with the execution card (`h-full`); here the sign-in box and the warnings can sit
              in the same column, so the card takes the height left over instead. */}
          <div className="flex min-w-0 flex-col [&>[role=region]]:flex-1">
            {view.status === "awaiting_approval" && (
              <p className={`mb-3 px-1 text-[12.5px] ${left > 0 && left < 60 ? "text-[var(--cp-danger-fg)]" : "text-vgray-400"}`} role="status">
                {left > 0 ? `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} left to approve` : "The approval window has closed. Ask your agent to draw the plan again."}
              </p>
            )}
            {open && !signer && (
              <PlanSignIn address={view.wallet} provider={view.provider ?? null} email={email} wallets={wallets} busy={running} error={connectError} onWallet={(id) => void connect(id)} />
            )}
            <ExecutionStepper
              steps={steps}
              currentStepIndex={Math.max(0, activeIndex)}
              network={network}
              cancelled={cancelled}
              busy={running}
              onSign={signer && !running && open ? () => void approve() : undefined}
              onRetry={open && signer && !running ? () => void approve() : undefined}
              onStop={open && !running ? () => void cancel() : undefined}
            />
            {view.warnings.length > 0 && open && (
              <ul className="mt-3 flex flex-col gap-1.5 rounded-2xl border border-vgray-100 bg-surface px-4 py-4 sm:px-5">
                {view.warnings.map((w) => (
                  <li key={w} className="flex gap-2 text-[13px] leading-5 text-vgray-500">
                    <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--cp-amber)]" aria-hidden="true" />{w}
                  </li>
                ))}
              </ul>
            )}
          </div>
          {hasDetails && <TransactionDetails rows={rows} health={health} settled={!open} />}
        </div>
      </ReviewOutcome>
      {!open && !completed && !cancelled && <DoneNote agent={agent} failed />}
    </PageShell>
  );
}
