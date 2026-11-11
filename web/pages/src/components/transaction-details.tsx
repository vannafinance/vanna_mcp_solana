import type { ReactNode } from "react";
// Copied from the Stellar MCP pages (sign-service/web/components/transaction-details.tsx).
import { AgentMark } from "./wallet-card";
import type { AgentIdentity } from "../lib/agents";

/**
 * The review page's frame around the Copilot's execution card: who asked (header), what the steps change on the
 * account (details panel, the way the website's Margin page shows a transaction), and a plain note once it is over.
 * The figures are Vanna's estimate when the review was prepared; the ledger decides the final numbers.
 */
export interface DetailRow {
  label: string;
  before: string;
  after: string;
  /** The change is good for the account (green), bad (red), or neither. */
  tone?: "better" | "worse" | "neutral";
}

const TONE = { better: "text-[var(--cp-ok-fg)]", worse: "text-[var(--cp-danger-fg)]", neutral: "text-vgray-900" } as const;

/** The website's health scale: 1.00 to 2.50+, liquidation at 1.10. */
const LIQUIDATION = 1.1;
const pos = (hf: number) => (Number.isFinite(hf) ? Math.max(0, Math.min(100, ((hf - 1) / 1.5) * 100)) : 100);
const zone = (hf: number) =>
  !Number.isFinite(hf) || hf >= 1.5 ? "text-[var(--cp-ok-fg)]" : hf >= 1.25 ? "text-[var(--cp-warn-fg)]" : "text-[var(--cp-danger-fg)]";

export function ReviewHeader({ agent, identity, steps, done, cancelled = false, aside }: {
  agent: string; identity: AgentIdentity | null; steps: number; done: boolean; cancelled?: boolean; aside?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="flex min-w-0 items-center gap-3.5">
        <AgentMark identity={identity} agent={agent} className="flex h-12 w-12 shrink-0 text-[22px]" />
        <div className="min-w-0">
          <h1 className="text-[24px] font-semibold leading-7 tracking-[-0.01em] text-vgray-900 sm:text-[26px]">
            {cancelled ? "Cancelled" : done ? "All set" : steps === 1 ? "Review and approve" : `Review ${steps} steps`}
          </h1>
          <p className="mt-0.5 text-[15.5px] leading-5 text-vgray-500">
            {done || cancelled
              ? `${agent} has the result.`
              : <>Requested by <span className="font-semibold text-vgray-700">{agent}</span>. Nothing moves until you approve it in your wallet.</>}
          </p>
        </div>
      </div>
      {aside}
    </header>
  );
}

/** Solana change: `before` is null for an account with no debt yet (no health factor), shown as "No debt". */
function HealthMeter({ before, after }: { before: number | null; after: number }) {
  const tick = pos(LIQUIDATION);
  return (
    <div className="pb-3 pt-1">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[14px] text-vgray-500">Health factor</span>
        <span className="flex items-baseline gap-1.5 font-mono tabular-nums">
          <span className="text-[14px] text-vgray-400">{before == null ? "No debt" : before.toFixed(2)}</span>
          <span className="text-[13px] text-vgray-300" aria-hidden="true">→</span>
          <span className={`text-[22px] font-semibold ${zone(after)}`}>{after.toFixed(2)}</span>
        </span>
      </div>
      <div className="relative mt-3 h-1.5 rounded-full" style={{ background: "linear-gradient(90deg, #e03e42 0%, #c98214 25%, #1c7c83 55%, #0b8f68 100%)" }}>
        <span className="absolute -top-1 h-3.5 w-px bg-vgray-900/60" style={{ left: `${tick}%` }} aria-hidden="true" />
        {before != null && <span className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-vgray-400 bg-surface" style={{ left: `${pos(before)}%` }} title={`Now ${before.toFixed(2)}`} />}
        <span className="absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-surface bg-vgray-900 shadow" style={{ left: `${pos(after)}%` }} title={`After ${after.toFixed(2)}`} />
      </div>
      <div className="mt-2 flex justify-between font-mono text-[12px] text-vgray-400">
        <span className="text-[var(--cp-danger-fg)]">1.10 liquidation</span>
        <span>2.50+</span>
      </div>
    </div>
  );
}

export function TransactionDetails({ rows, health, settled = false }: {
  rows: DetailRow[]; health?: { before: number | null; after: number } | null; settled?: boolean;
}) {
  if (!rows.length && !health) return null;
  return (
    <section className="flex h-full flex-col rounded-2xl border border-vgray-100 bg-surface px-4 py-5 sm:px-7 sm:py-6" aria-label="What changes">
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-[16.5px] font-semibold text-vgray-900">What changes</p>
        <p className="text-[13.5px] text-vgray-400">{settled ? "Before and after" : "If it goes through"}</p>
      </div>
      <div className="mt-3 flex flex-1 flex-col">
        {health && <HealthMeter before={health.before} after={health.after} />}
        <dl className="flex flex-col">
          {rows.map((row, index) => (
            <div key={row.label} className={`flex items-baseline justify-between gap-3 py-3 ${index < rows.length - 1 ? "border-b border-vgray-50" : ""}`}>
              <dt className="text-[14px] text-vgray-500">{row.label}</dt>
              <dd className="flex items-baseline gap-1.5 whitespace-nowrap font-mono text-[14px] tabular-nums">
                <span className="text-vgray-400">{row.before}</span>
                <span className="text-vgray-300" aria-hidden="true">→</span>
                <span className={`font-semibold ${TONE[row.tone ?? "neutral"]}`}>{row.after}</span>
              </dd>
            </div>
          ))}
        </dl>
      </div>
      <p className="mt-auto pt-3 text-[12.5px] leading-[17px] text-vgray-400">Vanna&apos;s estimate when this review was prepared. The chain decides the final figures.</p>
    </section>
  );
}

/** Shown once every step is final: the tab has nothing left to do, so it says so plainly instead of a dead button. */
export function DoneNote({ agent, failed = false }: { agent: string | null; failed?: boolean }) {
  return (
    <div className="mt-4 flex items-center gap-3 rounded-2xl border border-vgray-100 bg-surface px-5 py-4" role="status">
      <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full ${failed ? "bg-vgray-50 text-vgray-500" : "bg-[var(--cp-emerald)] text-white"}`} aria-hidden="true">
        {failed
          ? <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><path d="M5 12h14" /></svg>
          : <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>}
      </span>
      <div>
        <p className="text-[17.5px] font-semibold leading-6 text-vgray-900">{failed ? "Nothing more to do here" : "You can close this tab"}</p>
        <p className="text-[15.5px] leading-5 text-vgray-500">{agent ?? "Your agent"} already has the result.</p>
      </div>
    </div>
  );
}
