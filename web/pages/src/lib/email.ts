/**
 * Vanna's email / social wallet (Dynamic), as the pages see it: a small store the heavy Dynamic code (`dynamic.tsx`, loaded
 * on its own after the page shows) fills in. The same login as the website, so the same Solana wallet.
 */
import { useSyncExternalStore } from "react";
import { pageConfig } from "./runtime";
import type { Signer } from "./wallets";

export type SocialProvider = "google" | "twitter" | "github";

export interface EmailState {
  /** A Dynamic environment is configured for these pages. */
  enabled: boolean;
  /** The SDK has loaded (a stored session was restored or ruled out), or gave up. */
  ready: boolean;
  /** The SDK could not load (Dynamic unreachable, or this site is not an allowed origin in the environment). */
  failed: boolean;
  loggedIn: boolean;
  /** Logged in, but the Solana wallet is still being created (the person signed up on Stellar first). */
  creating: boolean;
  address: string | null;
  /** The email, else the social handle. */
  label: string | null;
}

export interface DynamicControls {
  sendCode: (email: string) => Promise<void>;
  verifyCode: (code: string) => Promise<void>;
  resendCode: () => Promise<void>;
  social: (provider: SocialProvider) => Promise<void>;
  logout: () => Promise<void>;
  signMessage: (text: string, expected: string) => Promise<Uint8Array>;
  signTransactions: (unsigned: string[], expected: string) => Promise<string[]>;
}

const enabled = () => !!pageConfig().dynamicEnv;
let state: EmailState = { enabled: false, ready: true, failed: false, loggedIn: false, creating: false, address: null, label: null };
let controls: DynamicControls | null = null;
const listeners = new Set<() => void>();

export function publishEmail(next: Partial<EmailState>) {
  const merged = { ...state, ...next };
  if (JSON.stringify(merged) === JSON.stringify(state)) return;
  state = merged;
  listeners.forEach((l) => l());
}
export function setDynamicControls(next: DynamicControls | null) { controls = next; }

export const emailState = (): EmailState => state;
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };
export const useEmailState = (): EmailState => useSyncExternalStore(subscribe, emailState, emailState);

/** Loads Dynamic in the background when the page has an environment. Safe to call more than once. */
let loading: Promise<void> | null = null;
export function startEmailWallet(): void {
  if (!enabled() || loading) return;
  state = { ...state, enabled: true, ready: false };
  const { dynamicEnv, dynamicRpc } = pageConfig();
  loading = import("./dynamic")
    .then((m) => m.mountDynamic(dynamicEnv, dynamicRpc))
    .catch((error: unknown) => {
      console.warn("Email sign-in could not load", error);
      publishEmail({ ready: true, failed: true });
    });
  // Dynamic never answered (unreachable, or this origin is not allowed): stop waiting so the page can say so.
  setTimeout(() => { if (!state.ready) publishEmail({ ready: true, failed: true }); }, 15_000);
}

const notReady = () => new Error("Email sign-in is still loading. Try again in a moment.");
function need(): DynamicControls { if (!controls) throw notReady(); return controls; }

export const emailActions = {
  sendCode: (email: string) => need().sendCode(email),
  verifyCode: (code: string) => need().verifyCode(code),
  resendCode: () => need().resendCode(),
  social: (provider: SocialProvider) => need().social(provider),
  logout: async () => { await controls?.logout(); },
};

/** The email wallet as a signer for `address` (the login's own Solana wallet). */
export function emailSigner(address: string): Signer {
  return {
    kind: "email",
    label: "Email wallet",
    address,
    async signMessage(text) {
      const bytes = await need().signMessage(text, address);
      let s = "";
      bytes.forEach((b) => { s += String.fromCharCode(b); });
      return btoa(s);
    },
    signTransactions: (unsigned) => need().signTransactions(unsigned, address),
  };
}

/**
 * A Google / X / GitHub sign-in leaves the page and comes back. Remember, for this page, that the person chose the email
 * wallet, so the page carries on by itself on return (the Stellar MCP's wallet page does the same).
 */
const intentKey = () => `vanna-email-intent:${location.pathname}|${new URLSearchParams(location.search).get("req") ?? ""}`;
export function markEmailIntent() { try { sessionStorage.setItem(intentKey(), "1"); } catch { /* storage blocked */ } }
export function takeEmailIntent(): boolean {
  try {
    const set = sessionStorage.getItem(intentKey()) === "1";
    sessionStorage.removeItem(intentKey());
    return set;
  } catch { return false; }
}
