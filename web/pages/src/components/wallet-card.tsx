"use client";

// Copied from the Stellar MCP pages (sign-service/web/components/wallet-card.tsx). Solana changes: the wallets are Phantom,
// Backpack and Solflare (from the page), no testnet-XLM top-up step, and the agent images are plain /static files.
import { useRef, useEffect, type FormEvent } from "react";
import { LOGOS as WALLET_LOGOS } from "../lib/logos";
import type { SocialProvider } from "../lib/email";
import { VannaMark } from "./page-shell";
import { identifyAgent, type AgentIdentity } from "../lib/agents";

/** What the card shows. The page that owns it decides the step from the Sign Service's answers. */
export type WalletStep = "loading" | "choose" | "code" | "confirm" | "creating" | "done" | "unavailable";

export interface WalletOption { id: string; label: string; logo: string; live: boolean; why?: string }

export interface WalletCardProps {
  mode: "signin" | "link";
  step: WalletStep;
  /** The agent's own name for itself (it chose it), and where the sign-in really returns to. */
  agent: string | null;
  redirectHost?: string;
  scopes?: string[];
  wallets: WalletOption[];
  emailEnabled: boolean;
  email: string;
  code: string;
  busy: boolean;
  /** Which social button was pressed, so only that one says it is opening. */
  opening: SocialProvider | null;
  resent: boolean;
  error: string;
  /** The wallet being asked to sign (step `confirm`). */
  confirming: { label: string; message: string } | null;
  /** Step `confirm` for a testnet account that holds no XLM yet: a top-up comes before the wallet is asked. */
  fund?: { busy: boolean; onFund: () => void; url: string } | null;
  doneAddress: string;
  returnHint: string;
  onEmail: (value: string) => void;
  onCode: (value: string) => void;
  onSendCode: () => void;
  onVerifyCode: () => void;
  onResend: () => void;
  onDifferentEmail: () => void;
  onSocial: (provider: SocialProvider) => void;
  onWallet: (id: string) => void;
  onUseAnother: () => void;
  onReturn: () => void;
  onCancel?: () => void;
}

/** Plain-language meaning of each permission; an unknown scope is shown as its own name. */
const SCOPE_TEXT: Record<string, string> = {
  "vanna:read": "See your balances, positions and market data",
  "vanna:write": "Prepare transactions for you. Each one waits for your approval.",
};

const MAIL = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" />
  </svg>
);
const GITHUB = (
  <svg width="15" height="15" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">
    <path d="M12 .5C5.65.5.5 5.65.5 12a11.5 11.5 0 0 0 7.86 10.92c.58.1.79-.25.79-.56v-2c-3.2.7-3.87-1.37-3.87-1.37-.52-1.33-1.28-1.69-1.28-1.69-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.19 1.76 1.19 1.03 1.76 2.69 1.25 3.35.96.1-.75.4-1.25.73-1.54-2.55-.29-5.24-1.28-5.24-5.69 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.17 1.18a11 11 0 0 1 5.77 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.84 1.19 3.1 0 4.42-2.7 5.39-5.26 5.68.41.36.78 1.06.78 2.14v3.17c0 .31.21.67.8.56A11.5 11.5 0 0 0 23.5 12C23.5 5.65 18.35.5 12 .5z" />
  </svg>
);
const X_MARK = (
  <svg width="13" height="13" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">
    <path d="M17.75 3h3.07l-6.7 7.66L22 21h-6.17l-4.83-6.32L5.47 21H2.4l7.17-8.2L2 3h6.33l4.37 5.78L17.75 3zm-1.08 16.17h1.7L7.4 4.74H5.58l11.09 14.43z" />
  </svg>
);
const SOCIALS: { id: SocialProvider; label: string; icon: React.ReactNode }[] = [
  // eslint-disable-next-line @next/next/no-img-element -- data-URI logo
  { id: "google", label: "Google", icon: <img src={WALLET_LOGOS.google} alt="" width={15} height={15} /> },
  { id: "twitter", label: "X", icon: X_MARK },
  { id: "github", label: "GitHub", icon: GITHUB },
];

/** The same wallet mark the execution card shows while a step waits for the wallet. */
function WalletWaitMark() {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className="text-violet-500" aria-hidden="true">
      <rect className="cp-exec-wallet-card" x="7.5" y="3" width="9" height="7" rx="1.4" />
      <path d="M6 8h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2Z" fill="var(--card)" />
      <path d="M20 13h-3a1.8 1.8 0 0 0 0 3.6h3" />
      <circle cx="17.2" cy="14.8" r=".7" fill="currentColor" stroke="none" />
    </svg>
  );
}

function Spinner() {
  return (
    <svg width="36" height="36" viewBox="0 0 24 24" fill="none" strokeWidth="2.4" strokeLinecap="round" className="animate-spin text-violet-500" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="var(--bar-track)" /><path d="M12 3a9 9 0 0 1 9 9" stroke="currentColor" />
    </svg>
  );
}

const CHECK = <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>;
const CROSS = <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>;

/**
 * Who is asking and what they get, beside the sign-in rather than inside it. The agent chose its own name; where the sign-in
 * returns is the real address, so both are shown, with what the agent can never do.
 */
/** The agent's own logo when its identity checks out (see `identifyAgent`), otherwise its initial. */
export function AgentMark({ identity, agent, className }: { identity: AgentIdentity | null; agent: string; className: string }) {
  if (!identity) {
    return <span className={`${className} items-center justify-center rounded-xl bg-[var(--violet-soft)] font-bold text-[var(--violet-ink)]`} aria-hidden="true">{agent.trim().charAt(0).toUpperCase() || "A"}</span>;
  }
  return (
    <span className={`${className} items-center justify-center rounded-xl border border-[var(--line-2)] bg-white`} style={identity.tile ? { background: identity.tile } : undefined} title={identity.label}>
      {identity.icon
        ? <svg viewBox="0 0 24 24" role="img" aria-label={identity.label} className="h-[55%] w-[55%]"><path d={identity.icon.path} fill={`#${identity.icon.hex}`} /></svg>
        // eslint-disable-next-line @next/next/no-img-element -- static export: imported so the URL carries the asset prefix
        : identity.image && <img src={identity.image} alt={identity.label} className="h-[62%] w-[62%] object-contain" />}
    </span>
  );
}

function RequestPanel({ mode, agent: declared, scopes, redirectHost }: { mode: "signin" | "link"; agent: string; scopes?: string[]; redirectHost?: string }) {
  const identity = identifyAgent(declared, redirectHost);
  const agent = identity?.label ?? declared;
  const can = mode === "signin" ? (scopes ?? []).map((scope) => SCOPE_TEXT[scope] ?? scope) : ["Use the wallet you pick for every request from now on"];
  const lists = (
    <>
      <p className="si-label mt-5">It can</p>
      <ul className="mt-2 flex flex-col gap-2">
        {can.map((text) => <li key={text} className="flex gap-2.5 text-[13px] leading-5 text-[var(--text)]"><span className="mt-0.5 text-[var(--ok)]">{CHECK}</span>{text}</li>)}
      </ul>
      <p className="si-label mt-4">It can&apos;t</p>
      <ul className="mt-2 flex flex-col gap-2">
        <li className="flex gap-2.5 text-[13px] leading-5 text-[var(--text)]"><span className="mt-0.5 text-[var(--danger)]">{CROSS}</span>Move funds without your approval. Every transaction opens your wallet first.</li>
        <li className="flex gap-2.5 text-[13px] leading-5 text-[var(--text)]"><span className="mt-0.5 text-[var(--danger)]">{CROSS}</span>See your email, password or private keys.</li>
      </ul>
    </>
  );
  return (
    <aside className="si-request" aria-label="Connection request">
      <p className="si-eyebrow hidden md:block">{mode === "signin" ? "Connection request" : "Wallet change"}</p>
      <div className="flex items-center gap-3 md:mt-4">
        <AgentMark identity={identity} agent={agent} className="flex h-9 w-9 shrink-0 text-[16px] md:h-14 md:w-14 md:text-[22px]" />
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="hidden text-[var(--quiet)] md:block" aria-hidden="true"><path d="M7 7h11l-3-3M17 17H6l3 3" /></svg>
        <span className="hidden h-14 items-center rounded-xl border border-[var(--line-2)] px-3 md:flex"><VannaMark className="h-10" /></span>
        {/* On a phone the name sits beside the mark, so the sign-in stays on the first screen. */}
        <div className="min-w-0 md:hidden">
          <p className="truncate text-[15px] font-bold text-[var(--title)]">{agent}</p>
          <p className="text-[12px] text-[var(--label)]">{mode === "signin" ? "wants to use your Vanna account" : "will switch to the wallet you pick"}</p>
        </div>
      </div>
      <h2 className="mt-4 hidden text-[18px] font-bold leading-6 text-[var(--title)] md:block">{agent}</h2>
      <p className="hidden text-[13px] text-[var(--label)] md:block">{mode === "signin" ? "wants to use your Vanna account" : "will switch to the wallet you pick"}</p>
      <div className="hidden md:block">{lists}</div>
      <details className="mt-3 md:hidden">
        <summary className="cursor-pointer text-[12.5px] font-semibold text-[var(--violet-ink)]">What {agent} can and can&apos;t do</summary>
        <div className="pb-1">{lists}</div>
      </details>
      {redirectHost && (
        <p className="si-return">You&apos;ll return to <b>{redirectHost}</b>. Continue only if you started this connection.</p>
      )}
    </aside>
  );
}

/**
 * The wallet page's card: the website's "Sign in to Vanna" dialog, as a page. Email code and Google / X / GitHub through
 * Dynamic, then the extension wallets. A sign-in also says which agent is asking, where it returns to, and what it may do.
 */
export function WalletCard(p: WalletCardProps) {
  const codeRef = useRef<HTMLInputElement>(null);
  useEffect(() => { if (p.step === "code") codeRef.current?.focus(); }, [p.step]);
  const agent = p.agent ?? "Your agent";
  const submit = (fn: () => void) => (e: FormEvent) => { e.preventDefault(); fn(); };

  const showRequest = !!p.agent && p.step !== "loading" && p.step !== "unavailable";
  return (
    <div className={`si mx-auto ${showRequest ? "si-wide" : ""}`}>
      {showRequest && <RequestPanel mode={p.mode} agent={agent} scopes={p.scopes} redirectHost={p.redirectHost} />}
      <div className="si-main">
      <div className="si-body">
        {p.step === "loading" && <div className="flex justify-center py-12"><Spinner /></div>}

        {p.step === "unavailable" && (
          <div className="py-6 text-center">
            <h1>This link can&apos;t be opened</h1>
            <p className="si-sub mt-2">{p.error || "Ask your agent to connect your wallet again."}</p>
          </div>
        )}

        {p.step === "choose" && (
          <>
            <h1>{p.mode === "signin" ? "Sign in to Vanna" : "Choose your wallet"}</h1>
            <p className="si-sub">{p.mode === "signin" ? "Use the same login as on vanna.finance: same account, same wallet." : "Sign in with the wallet you want to use from now on."}</p>

            {p.emailEnabled && (
              <>
                <form className="si-email" onSubmit={submit(p.onSendCode)}>
                  <label className="si-field">
                    {MAIL}
                    <input type="email" inputMode="email" autoComplete="email" placeholder="you@example.com" value={p.email} onChange={(e) => p.onEmail(e.target.value)} aria-label="Email" />
                  </label>
                  <button type="submit" className="si-go" disabled={p.busy || !p.email.trim()}>{p.busy && !p.opening ? "Sending code…" : "Continue"}</button>
                </form>
                <div className="si-social">
                  {SOCIALS.map((s) => (
                    <button key={s.id} type="button" className="si-soc" disabled={p.busy} onClick={() => p.onSocial(s.id)} title={`Sign in with ${s.label}`}>
                      {s.icon}{p.opening === s.id ? `Opening ${s.label}…` : s.label}
                    </button>
                  ))}
                </div>
              </>
            )}
            {p.error && <p className="si-err" role="alert">{p.error}</p>}
            <div className="si-or"><span>{p.emailEnabled ? "or connect a wallet" : "connect a wallet"}</span></div>
            <div className="si-wallets">
              {p.wallets.map((w) => (
                <button key={w.id} type="button" className={`si-w ${w.live ? "live" : ""}`} disabled={!w.live || p.busy} title={w.why ?? `Connect ${w.label}`} onClick={() => p.onWallet(w.id)}>
                  {/* eslint-disable-next-line @next/next/no-img-element -- data-URI logo */}
                  <img src={w.logo} alt="" width={40} height={40} />
                  <span>{w.label}</span>
                  {!w.live && <span className="si-soon">{w.why ? "Get it" : "Soon"}</span>}
                </button>
              ))}
            </div>
            {p.onCancel && <button type="button" className="mt-4 w-full text-center text-[12px] font-semibold text-[var(--label)] hover:text-[var(--text)]" onClick={p.onCancel}>Cancel and go back</button>}
          </>
        )}

        {p.step === "code" && (
          <>
            <h1>Check your email</h1>
            <form className="si-email" onSubmit={submit(p.onVerifyCode)}>
              <p className="si-sent">Enter the 6-digit code sent to <b>{p.email.trim()}</b></p>
              <label className="si-field si-code">
                <input ref={codeRef} inputMode="numeric" autoComplete="one-time-code" maxLength={6} placeholder="000000" value={p.code}
                  onChange={(e) => p.onCode(e.target.value.replace(/\D/g, "").slice(0, 6))} aria-label="Sign-in code" />
              </label>
              <button type="submit" className="si-go" disabled={p.busy || p.code.length !== 6}>{p.busy ? "Signing in…" : "Sign in"}</button>
              <div className="si-links">
                <button type="button" onClick={p.onDifferentEmail}>Use a different email</button>
                <button type="button" onClick={p.onResend} disabled={p.resent}>{p.resent ? "Code sent" : "Send a new code"}</button>
              </div>
            </form>
            {p.error && <p className="si-err" role="alert">{p.error}</p>}
          </>
        )}

        {p.step === "creating" && (
          <div className="flex flex-col items-center py-8 text-center">
            <Spinner />
            <h1 className="mt-4">Setting up your wallet</h1>
            <p className="si-sub">This takes a few seconds the first time.</p>
          </div>
        )}

        {p.step === "confirm" && p.confirming && (
          <div className="flex flex-col items-center py-6 text-center">
            <WalletWaitMark />
            <h1 className="mt-4">{p.fund ? "Add testnet XLM first" : `Confirm in ${p.confirming.label}`}</h1>
            <p className="si-sub max-w-[300px]">{p.confirming.message}</p>
            {p.fund && (
              <>
                <button type="button" className="si-go mt-5 w-full max-w-[260px]" disabled={p.fund.busy} onClick={p.fund.onFund}>
                  {p.fund.busy ? "Adding testnet XLM…" : "Get free testnet XLM"}
                </button>
                <p className="mt-4 max-w-[320px] text-[11.5px] leading-5 text-[var(--label)]">
                  Or open this link yourself, then come back and press Connect again:
                </p>
                <a href={p.fund.url} target="_blank" rel="noreferrer" className="mt-1 max-w-[320px] break-all font-mono text-[11px] text-[var(--violet-ink)] underline">{p.fund.url}</a>
              </>
            )}
            {p.error && <p className="si-err" role="alert">{p.error}</p>}
            <button type="button" className="mt-5 text-[12px] font-semibold text-[var(--violet-ink)]" onClick={p.onUseAnother}>Use another wallet</button>
          </div>
        )}

        {p.step === "done" && (
          <div className="flex flex-col items-center py-6 text-center">
            <span className="cp-exec-pop flex h-11 w-11 items-center justify-center rounded-full bg-[var(--cp-emerald)] text-white" aria-hidden="true">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
            </span>
            <h1 className="mt-4">You&apos;re connected</h1>
            <p className="si-sub">{agent} now uses this wallet.</p>
            {p.doneAddress && <p className="mt-3 rounded-lg bg-[var(--violet-soft)] px-3 py-1.5 font-mono text-[12px] font-semibold text-[var(--violet-ink)]">{p.doneAddress.slice(0, 6)}…{p.doneAddress.slice(-6)}</p>}
            <button type="button" className="si-go mt-5 w-full" onClick={p.onReturn}>{p.mode === "signin" ? `Continue to ${agent}` : `Close tab and return to ${agent}`}</button>
            {p.returnHint && <p className="mt-2 text-[12px] text-[var(--label)]" role="status">{p.returnHint}</p>}
          </div>
        )}
      </div>
      {p.emailEnabled && (
        <div className="si-foot">
          {/* eslint-disable-next-line @next/next/no-img-element -- data-URI logo */}
          <img src={WALLET_LOGOS.dynamic} alt="" width={16} height={16} />
          Powered by Dynamic
        </div>
      )}
      </div>
    </div>
  );
}
