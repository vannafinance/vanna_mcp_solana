"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
// Copied from the Stellar MCP pages (sign-service/web/components/done-card.tsx); transaction links go to the Solana explorer.
import type { StepperStep } from "./execution-stepper";
import { explorerTx } from "../lib/runtime";

/** How long the finished card stays on screen, every step ticked, before the page turns into the close-tab card. */
const HOLD_MS = 1100;
/** Matches .rv-leave in globals.css. */
const LEAVE_MS = 260;
const reducedMotion = () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Once every step is final, the whole review (execution card and "What changes") fades out together and the close-tab
 * card takes its place: a ring draws and fills, the tick draws, and the result rises in. With reduced motion the
 * close-tab card simply replaces the review.
 */
export function ReviewOutcome({ over, children, done }: { over: boolean; children: ReactNode; done: ReactNode }) {
  // Opened on a plan that was already over: nothing just happened here, so the card shows without the motion.
  const overOnOpen = useRef(over);
  const [phase, setPhase] = useState<"review" | "leaving" | "done">(over ? "done" : "review");
  useEffect(() => {
    if (!over) { overOnOpen.current = false; setPhase("review"); return; }
    if (overOnOpen.current) return;
    const still = reducedMotion();
    const leave = window.setTimeout(() => setPhase(still ? "done" : "leaving"), still ? 0 : HOLD_MS);
    const finish = window.setTimeout(() => setPhase("done"), still ? 0 : HOLD_MS + LEAVE_MS);
    return () => { window.clearTimeout(leave); window.clearTimeout(finish); };
  }, [over]);
  if (phase === "done") return <div className="mx-auto w-full max-w-[520px]">{done}</div>;
  return <div className={phase === "leaving" ? "rv-leave pointer-events-none" : undefined}>{children}</div>;
}

/**
 * The close-tab card. "done": every step went through (green, a tick). "cancelled": the person stopped the rest (grey,
 * a cross): nothing further is sent, and steps that already settled stay listed, because cancelling cannot undo them.
 */
export function DoneCard({ agent, steps, network, outcome = "done" }: {
  agent: string | null; steps: StepperStep[]; network: string; outcome?: "done" | "cancelled";
}) {
  const cancelled = outcome === "cancelled";
  void network;
  const settled = steps.filter((s) => s.status === "settled");
  const unsent = steps.length - settled.length;
  return (
    <section className="rv-card flex flex-col items-center justify-center rounded-2xl border border-vgray-100 bg-surface px-6 py-8 text-center" role="status" aria-live="polite">
      <span className="relative flex h-16 w-16 items-center justify-center" aria-hidden="true">
        <span className={`rv-ripple absolute inset-0 rounded-full ${cancelled ? "bg-[var(--cp-danger-fg)]" : "bg-[var(--cp-emerald)]"}`} />
        <svg viewBox="0 0 64 64" className="relative h-16 w-16">
          <circle cx="32" cy="32" r="29" className={`rv-fill ${cancelled ? "fill-[var(--cp-danger-fg)]" : "fill-[var(--cp-emerald)]"}`} />
          <circle cx="32" cy="32" r="29" fill="none" strokeWidth="3" className={`rv-ring ${cancelled ? "stroke-[var(--cp-danger-fg)]" : "stroke-[var(--cp-emerald)]"}`} />
          {cancelled
            ? <>
                <path d="M23 23l18 18" fill="none" stroke="#fff" strokeWidth="4.5" strokeLinecap="round" className="rv-cross-a" />
                <path d="M41 23L23 41" fill="none" stroke="#fff" strokeWidth="4.5" strokeLinecap="round" className="rv-cross-b" />
              </>
            : <path d="M20 33l8 8 16-17" fill="none" stroke="#fff" strokeWidth="4.5" strokeLinecap="round" strokeLinejoin="round" className="rv-tick" />}
        </svg>
      </span>
      <h2 className="rv-rise mt-5 text-[20px] font-semibold leading-7 tracking-[-0.01em] text-vgray-900 [animation-delay:900ms]">
        {cancelled ? "Cancelled" : "You can close this tab"}
      </h2>
      <p className="rv-rise mt-1 max-w-[360px] text-[14px] leading-5 text-vgray-500 [animation-delay:980ms]">
        {cancelled
          ? <>{unsent === 1 ? "1 step was" : `${unsent} steps were`} not sent, and nothing more will be. {agent ?? "Your agent"} already knows. You can close this tab.</>
          : <>{agent ?? "Your agent"} already has the result.</>}
      </p>
      {cancelled && settled.length > 0 && (
        <p className="rv-rise mt-5 text-[12.5px] text-vgray-400 [animation-delay:1040ms]">Already on the chain, and kept:</p>
      )}
      {settled.length > 0 && (
        <ul className={`${cancelled ? "mt-2" : "mt-5"} flex w-full max-w-[360px] flex-col border-t border-vgray-50 pt-3 text-left`}>
          {settled.map((step, index) => (
            <li key={step.id} className="rv-rise flex items-center justify-between gap-3 py-1.5" style={{ animationDelay: `${1060 + index * 70}ms` }}>
              <span className="flex min-w-0 items-center gap-2 text-[13px] text-vgray-700">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-[var(--cp-ok-fg)]" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
                <span className="truncate">{step.label}</span>
              </span>
              {step.txHash && (
                <a href={explorerTx(step.txHash)} target="_blank" rel="noreferrer" className="shrink-0 font-mono text-[12px] text-violet-500 hover:text-violet-600">
                  {step.txHash.slice(0, 6)}… ↗
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
