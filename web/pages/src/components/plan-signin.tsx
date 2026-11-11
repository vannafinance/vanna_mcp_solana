/**
 * The review page's sign-in, when this browser has no wallet for the plan yet: the Stellar MCP's EmailLogin box
 * (sign-service/web/components/email-login.tsx), plus the extension wallets, because a Solana plan link does not say which
 * kind of wallet owns it. The plan is for one address; whatever signs must be that address (the server checks it too).
 */
import { useState } from "react";
import { LOGOS } from "../lib/logos";
import { emailActions, markEmailIntent, type EmailState, type SocialProvider } from "../lib/email";
import { short } from "../lib/runtime";
import type { WalletEntry } from "../lib/wallets";

const SOCIALS: { id: SocialProvider; label: string }[] = [
  { id: "google", label: "Google" }, { id: "twitter", label: "X" }, { id: "github", label: "GitHub" },
];

export function PlanSignIn({ address, provider, email, wallets, busy, error, onWallet }: {
  address: string; provider: string | null; email: EmailState; wallets: (WalletEntry & { live: boolean })[]; busy: boolean; error: string;
  onWallet: (id: string) => void;
}) {
  // The sign-in's own wallet first (it could not connect by itself: locked, or not yet allowed on this site).
  const ordered = provider && provider !== "dynamic"
    ? [...wallets].sort((a, b) => Number(b.label.toLowerCase() === provider.toLowerCase()) - Number(a.label.toLowerCase() === provider.toLowerCase()))
    : wallets;
  const extension = provider && provider !== "dynamic" ? provider : null;
  const [mail, setMail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [working, setWorking] = useState(false);
  const [problem, setProblem] = useState("");
  const run = async (work: () => Promise<void>, fallback: string) => {
    setWorking(true); setProblem("");
    try { await work(); } catch (e) { setProblem(e instanceof Error && e.message ? e.message : fallback); } finally { setWorking(false); }
  };
  const emailOn = email.enabled && !email.failed;
  const wrongEmail = email.loggedIn && !!email.address && email.address !== address;
  const disabled = busy || working;
  return (
    <div className="mb-3 flex flex-col gap-2 rounded-2xl border border-vgray-100 bg-surface px-4 py-4 sm:px-5">
      <p className="text-[15px] font-semibold text-vgray-900">Sign in to approve</p>
      <p className="text-[13px] text-vgray-500">
        {extension
          ? <>These steps are for your {extension} wallet <span className="font-mono font-semibold text-vgray-700">{short(address, 4, 4)}</span>. Unlock {extension} and connect it.</>
          : provider === "dynamic"
            ? <>These steps are for your email wallet <span className="font-mono font-semibold text-vgray-700">{short(address, 4, 4)}</span>. Sign in with the same email, Google, X or GitHub account.</>
            : <>These steps are for <span className="font-mono font-semibold text-vgray-700">{short(address, 4, 4)}</span>. Sign in with the email that owns it, or connect that wallet.</>}
      </p>
      {emailOn && wrongEmail && (
        <div className="flex flex-col gap-2 text-[13px] text-vgray-500">
          <p>This email is signed in to a different wallet ({short(email.address!)}). Sign in with the email that owns {short(address)}.</p>
          <button type="button" className="self-start font-semibold text-violet-500" onClick={() => void emailActions.logout()}>Sign out</button>
        </div>
      )}
      {emailOn && !wrongEmail && !email.loggedIn && (
        <>
          {!sent ? (
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void run(async () => { await emailActions.sendCode(mail.trim()); setSent(true); }, "Could not send the code. Try again."); }}>
              <input type="email" autoComplete="email" placeholder="you@example.com" value={mail} onChange={(e) => setMail(e.target.value)} aria-label="Email"
                className="h-10 min-w-0 flex-1 rounded-lg border border-vgray-200 bg-surface px-3 text-[14px] text-vgray-900 outline-none focus:border-violet-500" />
              <button type="submit" disabled={disabled || !mail.trim()} className="h-10 rounded-lg bg-[image:var(--cp-gradient)] px-4 text-[13px] font-semibold text-white disabled:opacity-50">Send code</button>
            </form>
          ) : (
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void run(() => emailActions.verifyCode(code), "That code did not work. Check it or send a new one."); }}>
              <input inputMode="numeric" autoComplete="one-time-code" maxLength={6} placeholder="6-digit code" value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} aria-label="Sign-in code"
                className="h-10 min-w-0 flex-1 rounded-lg border border-vgray-200 bg-surface px-3 text-center font-mono text-[15px] tracking-[0.3em] text-vgray-900 outline-none focus:border-violet-500" />
              <button type="submit" disabled={disabled || code.length !== 6} className="h-10 rounded-lg bg-[image:var(--cp-gradient)] px-4 text-[13px] font-semibold text-white disabled:opacity-50">Sign in</button>
            </form>
          )}
          {!sent && (
            <div className="flex gap-2">
              {SOCIALS.map((s) => (
                <button key={s.id} type="button" disabled={disabled} onClick={() => { markEmailIntent(); void run(() => emailActions.social(s.id), "Could not start that sign-in. Try again."); }}
                  className="flex h-9 flex-1 items-center justify-center gap-1.5 rounded-lg border border-vgray-200 text-[12px] font-semibold text-vgray-700 hover:border-violet-500">
                  {s.id === "google" && <img src={LOGOS.google} alt="" width={14} height={14} />}
                  {s.label}
                </button>
              ))}
            </div>
          )}
        </>
      )}
      {emailOn && email.creating && <p className="text-[12px] text-vgray-400" role="status">Setting up your wallet…</p>}
      {emailOn && !email.ready && <p className="text-[12px] text-vgray-400" role="status">Checking your email sign-in…</p>}
      <div className="si-or"><span>{emailOn ? "or connect a wallet" : "connect a wallet"}</span></div>
      <div className="si-wallets">
        {ordered.map((w) => (
          <button key={w.id} type="button" className={`si-w ${w.live ? "live" : ""}`} disabled={!w.live || disabled}
            title={w.live ? `Connect ${w.label}` : `${w.label} is not installed in this browser`} onClick={() => onWallet(w.id)}>
            <img src={w.logo} alt="" width={40} height={40} />
            <span>{w.label}</span>
            {!w.live && <span className="si-soon">Get it</span>}
          </button>
        ))}
      </div>
      {(problem || error) && <p className="si-err" role="alert">{problem || error}</p>}
    </div>
  );
}

/** The wallet a review must be signed with (Stellar's WalletBadge): its kind once connected, and the address. */
export function WalletBadge({ label, logo, address }: { label: string; logo: string; address: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border border-vgray-100 bg-surface py-1 pl-1 pr-2.5 text-[12px] font-semibold text-vgray-700" title={address}>
      <img src={logo} alt="" width={18} height={18} className="rounded-full" />
      {label}
      <span className="font-mono font-medium text-vgray-400">{short(address)}</span>
    </span>
  );
}
