/**
 * `/connect?req=…`: the MCP login. An MCP client (Claude, Cursor, Codex …) asked to use the person's Vanna account; the person
 * signs in with the same email / Google / X / GitHub login as the website (Dynamic) or with a Solana wallet extension, and
 * that wallet signs the one message the server issued (Sign-In-With-Solana). The server decides everything: what is being
 * authorized, the message, whether the signature verifies (mcp_server/oauth/consent.py). Laid out as the Stellar MCP's
 * wallet page (WalletCard), so both MCPs sign in the same way.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { PageShell } from "../components/page-shell";
import { WalletCard, type WalletOption, type WalletStep } from "../components/wallet-card";
import { emailActions, emailSigner, markEmailIntent, takeEmailIntent, useEmailState, type SocialProvider } from "../lib/email";
import { pageConfig } from "../lib/runtime";
import { connectExtension, isMobile, mobileLinks, onWalletsChange, walletList, type Signer } from "../lib/wallets";
import { SiteWarning } from "./site-warning";

interface Info { client_name: string; redirect_host: string; requested_scopes: string[] }

const req = new URLSearchParams(location.search).get("req") ?? "";

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store" });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.message || `Request failed (${r.status})`);
  return data as T;
}

const errorText = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

export function ConnectPage() {
  const email = useEmailState();
  const [info, setInfo] = useState<Info | null>(null);
  const [step, setStep] = useState<WalletStep>("loading");
  const [error, setError] = useState("");
  const [address, setAddress] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [opening, setOpening] = useState<SocialProvider | null>(null);
  const [resent, setResent] = useState(false);
  const [confirming, setConfirming] = useState<{ label: string; message: string } | null>(null);
  const [doneAddress, setDoneAddress] = useState("");
  const [redirectUrl, setRedirectUrl] = useState("");
  const [, setTick] = useState(0);
  const proving = useRef(false);
  // The person chose the email wallet here (a code, or back from Google / X / GitHub): prove it once the wallet exists.
  const wantsEmail = useRef(false);

  useEffect(() => onWalletsChange(() => setTick((t) => t + 1)), []);
  useEffect(() => {
    if (!req) { setError("This page needs a sign-in request from your app. Start the connection again from your MCP client."); setStep("unavailable"); return; }
    wantsEmail.current = takeEmailIntent();
    fetch(`/oauth/consent/request?req=${encodeURIComponent(req)}`, { cache: "no-store" })
      .then(async (r) => {
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.message || "This sign-in request is not valid.");
        setInfo(data as Info);
        setStep(wantsEmail.current ? "creating" : "choose");
      })
      .catch((e) => { setError(errorText(e, "This sign-in request is not valid.")); setStep("unavailable"); });
  }, []);

  /** The chosen wallet signs the server's message; the server checks it and sends the browser back to the app. */
  const prove = useCallback(async (signer: Signer, label: string, message: string) => {
    if (proving.current || !info) return;
    proving.current = true; setBusy(true); setError("");
    setConfirming({ label, message }); setStep("confirm");
    try {
      const ch = await post<{ message: string; nonce: string }>("/oauth/consent/challenge", { req, wallet: signer.address, scopes: info.requested_scopes });
      const signature = await signer.signMessage(ch.message);
      // Which wallet signed in (the email / social wallet, or the extension's name): plans then open with it ready.
      const provider = signer.kind === "email" ? "dynamic" : signer.label;
      const done = await post<{ redirect_url: string }>("/oauth/consent/complete", { req, wallet: signer.address, nonce: ch.nonce, signature, provider });
      setDoneAddress(signer.address); setStep("done"); setRedirectUrl(done.redirect_url);
      window.location.assign(done.redirect_url);
    } catch (e) {
      setError(errorText(e, "The wallet could not be connected. Try again."));
      setStep("choose");
    } finally { proving.current = false; setBusy(false); }
  }, [info]);

  // Email: once Dynamic's login is done and its Solana wallet exists, the proof follows straight away.
  useEffect(() => {
    if (!wantsEmail.current || !info || step === "done" || step === "unavailable") return;
    if (email.ready && email.failed) { wantsEmail.current = false; setError("Email sign-in is not available on this page right now. Use a wallet extension."); setStep("choose"); return; }
    if (!email.loggedIn) return;
    if (!email.address) { setStep("creating"); return; }
    wantsEmail.current = false;
    void prove(emailSigner(email.address), "your email wallet",
      "Approve the signature for your email wallet. It only proves this wallet is yours: nothing is sent and there is no fee.");
  }, [email, info, step, prove]);

  const run = async (work: () => Promise<void>, fallback: string) => {
    setBusy(true); setError("");
    try { await work(); } catch (e) { setError(errorText(e, fallback)); } finally { setBusy(false); }
  };

  const onSendCode = () => run(async () => {
    if (email.loggedIn) await emailActions.logout(); // a code is for the email typed here, not an older session
    await emailActions.sendCode(address.trim());
    setCode(""); setStep("code");
  }, "Could not send the code. Try again.");

  const onVerifyCode = () => run(async () => {
    wantsEmail.current = true;
    await emailActions.verifyCode(code);
    setStep("creating");
  }, "That code did not work. Check it or send a new one.");

  const onResend = () => run(async () => {
    await emailActions.resendCode();
    setResent(true); window.setTimeout(() => setResent(false), 4000);
  }, "Could not send a new code.");

  const onSocial = (provider: SocialProvider) => {
    setOpening(provider);
    markEmailIntent();
    void run(async () => {
      if (email.loggedIn) await emailActions.logout();
      await emailActions.social(provider);
    }, "Could not start that sign-in. Try again.").finally(() => setOpening(null));
  };

  const onWallet = (id: string) => {
    const entry = walletList().find((w) => w.id === id);
    if (entry && !entry.installed && isMobile()) {
      const link = mobileLinks().find((l) => l.label === entry.label);
      if (link) { window.location.href = link.href; return; }
    }
    void run(async () => {
      const signer = await connectExtension(id);
      await prove(signer, signer.label, `Sign the message ${signer.label} shows. It only proves this wallet is yours: nothing is sent and there is no fee.`);
    }, "The wallet could not be connected. Try again.");
  };

  const onCancel = () => void run(async () => {
    const done = await post<{ redirect_url: string }>("/oauth/consent/deny", { req });
    window.location.assign(done.redirect_url);
  }, "Could not decline. Close this tab instead.");

  const mobile = isMobile();
  const wallets: WalletOption[] = walletList().map((w) => ({
    id: w.id, label: w.label, logo: w.logo, live: w.installed || mobile,
    why: w.installed || mobile ? undefined : `${w.label} is not installed in this browser`,
  }));
  const emailEnabled = email.enabled && !email.failed;

  return (
    <PageShell wide network={pageConfig().test ? "testnet" : "mainnet"}>
      <SiteWarning />
      <WalletCard
        mode="signin"
        step={step}
        agent={info ? info.client_name : null}
        redirectHost={info?.redirect_host}
        scopes={info?.requested_scopes}
        wallets={wallets}
        emailEnabled={emailEnabled}
        email={address}
        code={code}
        busy={busy}
        opening={opening}
        resent={resent}
        error={error}
        confirming={confirming}
        doneAddress={doneAddress}
        returnHint={redirectUrl ? "Returning to your app…" : ""}
        onEmail={setAddress}
        onCode={setCode}
        onSendCode={() => void onSendCode()}
        onVerifyCode={() => void onVerifyCode()}
        onResend={() => void onResend()}
        onDifferentEmail={() => { setStep("choose"); setError(""); setCode(""); }}
        onSocial={onSocial}
        onWallet={onWallet}
        onUseAnother={() => { wantsEmail.current = false; setStep("choose"); setError(""); }}
        onReturn={() => { if (redirectUrl) window.location.assign(redirectUrl); }}
        onCancel={onCancel}
      />
    </PageShell>
  );
}
