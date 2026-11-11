/**
 * The browser-extension Solana wallets (Phantom, Backpack, Solflare and any other Wallet Standard wallet), with the older
 * injected objects as a fallback. No keys are ever handled here: the wallet signs, this file only asks it to. Signatures and
 * transactions travel as base64, the way the server takes them.
 */
import { VersionedTransaction } from "@solana/web3.js";
import { Buffer } from "buffer";
import { LOGOS } from "./logos";

type Features = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
interface StandardWallet { name: string; icon?: string; chains?: string[]; features?: Features; accounts?: { address: string; chains?: string[] }[] }
interface Legacy { connect: () => Promise<{ publicKey?: { toString(): string } } | void>; publicKey?: { toString(): string };
  signMessage: (m: Uint8Array, enc?: string) => Promise<{ signature?: Uint8Array } | Uint8Array>;
  signTransaction: (t: VersionedTransaction) => Promise<VersionedTransaction>;
  signAllTransactions?: (t: VersionedTransaction[]) => Promise<VersionedTransaction[]> }

const standard: StandardWallet[] = [];
function register(...wallets: StandardWallet[]) {
  for (const w of wallets) {
    const solana = w?.chains?.some((c) => String(c).startsWith("solana:"));
    const f = w?.features ?? {};
    if (solana && f["standard:connect"] && f["solana:signTransaction"] && f["solana:signMessage"] && !standard.includes(w)) standard.push(w);
  }
  listeners.forEach((l) => l());
  return () => undefined;
}
const listeners = new Set<() => void>();
/** Calls `listener` when a wallet announces itself (they register a moment after the page loads). */
export function onWalletsChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
// Injected wallets (window.phantom, window.solflare …) can appear after this script runs: look again once the page has loaded.
const notify = () => listeners.forEach((l) => l());
if (document.readyState === "complete") setTimeout(notify, 300);
else window.addEventListener("load", () => { notify(); setTimeout(notify, 1000); }, { once: true });
try {
  const api = Object.freeze({ register });
  window.addEventListener("wallet-standard:register-wallet", (e) => { try { (e as CustomEvent).detail(api); } catch { /* a broken wallet is skipped */ } });
  window.dispatchEvent(new CustomEvent("wallet-standard:app-ready", { detail: api }));
} catch { /* very old browser: the injected fallback still works */ }

function legacy(): { name: string; p: Legacy }[] {
  const w = window as unknown as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const found: { name: string; p: Legacy }[] = [];
  if (w.phantom?.solana?.isPhantom) found.push({ name: "Phantom", p: w.phantom.solana });
  if (w.backpack && (w.backpack.isBackpack || w.backpack.connect)) found.push({ name: "Backpack", p: w.backpack });
  if (w.solflare?.isSolflare) found.push({ name: "Solflare", p: w.solflare });
  return found;
}

export interface WalletEntry { id: string; label: string; logo: string; installed: boolean }

/** The three wallets the website offers, always shown (installed or not), then any other Solana wallet found here. */
const KNOWN = [
  { id: "phantom", label: "Phantom", logo: LOGOS.phantom },
  { id: "backpack", label: "Backpack", logo: LOGOS.backpack },
  { id: "solflare", label: "Solflare", logo: LOGOS.solflare },
];

function find(label: string): { std?: StandardWallet; p?: Legacy } | null {
  const std = standard.find((w) => w.name.toLowerCase() === label.toLowerCase());
  if (std) return { std };
  const old = legacy().find((x) => x.name.toLowerCase() === label.toLowerCase());
  return old ? { p: old.p } : null;
}

export function walletList(): WalletEntry[] {
  const out: WalletEntry[] = KNOWN.map((k) => ({ ...k, installed: !!find(k.label) }));
  for (const w of standard) {
    if (KNOWN.some((k) => k.label.toLowerCase() === w.name.toLowerCase())) continue;
    const icon = w.icon && /^data:image\/(svg\+xml|png|webp|jpeg);base64,/.test(w.icon) ? w.icon : LOGOS.dynamic;
    out.push({ id: `std:${w.name}`, label: w.name, logo: icon, installed: true });
  }
  return out;
}

export const isMobile = () => /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "");

/** Open this same page inside a wallet's own browser (phones without an extension). */
export function mobileLinks(): { label: string; href: string }[] {
  const url = encodeURIComponent(location.href), ref = encodeURIComponent(location.origin);
  return [
    { label: "Phantom", href: `https://phantom.app/ul/browse/${url}?ref=${ref}` },
    { label: "Solflare", href: `https://solflare.com/ul/v1/browse/${url}?ref=${ref}` },
    { label: "Backpack", href: `https://backpack.app/ul/v1/browse/${url}?ref=${ref}` },
  ];
}

const REJECTED = /user rejected|rejected the request|user denied|denied by the user|user cancel|declined|4001/i;
function declined(e: unknown, what: string): never {
  const text = e instanceof Error ? `${e.name} ${e.message}` : String(e);
  if (REJECTED.test(text)) throw new Error(what);
  throw e instanceof Error ? e : new Error(text);
}

const b64 = (bytes: Uint8Array) => Buffer.from(bytes).toString("base64");

/** A connected extension wallet: its address and the two things the pages ask of it. */
export interface Signer {
  kind: "extension" | "email";
  label: string;
  address: string;
  /** Signs `text`; returns the 64-byte signature as base64. */
  signMessage: (text: string) => Promise<string>;
  /** Signs serialized transactions (base64) in one approval where the wallet allows it; returns them signed, base64. */
  signTransactions: (unsigned: string[]) => Promise<string[]>;
}

/**
 * Connects the wallet the person already allowed on this site, without a popup (Wallet Standard `silent`, the older
 * `onlyIfTrusted`), and only if it is on `expected`: how the plan page opens with the sign-in's wallet ready, as the
 * Stellar MCP's review page does. `label` names the wallet to try first (the one the sign-in used); the other installed
 * wallets are tried after it. Null when none is allowed here, unlocked, and on that address.
 */
export async function connectSilently(expected: string, label?: string | null): Promise<Signer | null> {
  const names = walletList().filter((w) => w.installed).map((w) => w.label);
  const order = label ? [label, ...names.filter((n) => n.toLowerCase() !== label.toLowerCase())] : names;
  for (const name of order) {
    const entry = walletList().find((w) => w.label.toLowerCase() === name.toLowerCase() && w.installed);
    if (!entry) continue;
    try {
      const signer = await connectExtension(entry.id, true);
      if (signer.address === expected) return signer;
    } catch { /* not allowed here yet, locked, or declined silently: try the next */ }
  }
  return null;
}

export async function connectExtension(id: string, silent = false): Promise<Signer> {
  const entry = walletList().find((w) => w.id === id);
  if (!entry) throw new Error("That wallet is no longer available. Reload the page.");
  const found = find(entry.label);
  if (!found) throw new Error(`${entry.label} is not installed in this browser. Install it and reload, or choose another way to sign in.`);
  let address: string;
  let account: { address: string; chains?: string[] } | null = null;
  if (found.std) {
    const std = found.std;
    const res = await std.features!["standard:connect"].connect(silent ? { silent: true } : undefined).catch((e: unknown) => declined(e, "You declined the connection in your wallet."));
    const accounts: { address: string; chains?: string[] }[] = (res?.accounts?.length ? res.accounts : std.accounts) ?? [];
    account = accounts.find((a) => (a.chains ?? []).some((c) => String(c).startsWith("solana:"))) ?? accounts[0] ?? null;
    if (!account) throw new Error("The wallet did not return an address.");
    address = account.address;
  } else {
    const p = found.p!;
    const r = await (silent ? (p.connect as (o: { onlyIfTrusted: boolean }) => ReturnType<Legacy["connect"]>)({ onlyIfTrusted: true }) : p.connect())
      .catch((e: unknown) => declined(e, "You declined the connection in your wallet."));
    const key = (r && r.publicKey) || p.publicKey;
    if (!key) throw new Error("The wallet did not return an address.");
    address = key.toString();
  }
  return {
    kind: "extension",
    label: entry.label,
    address,
    async signMessage(text) {
      const bytes = new TextEncoder().encode(text);
      try {
        if (found.std) {
          const out = await found.std.features!["solana:signMessage"].signMessage({ account, message: bytes });
          return b64(new Uint8Array(out[0].signature));
        }
        const res = await found.p!.signMessage(bytes, "utf8");
        const sig = res instanceof Uint8Array ? res : res.signature;
        return b64(new Uint8Array(sig as Uint8Array));
      } catch (e) { return declined(e, "You declined the signature in your wallet. Nothing was approved."); }
    },
    async signTransactions(unsigned) {
      const what = unsigned.length > 1 ? "You declined the transactions in your wallet. Nothing was sent." : "You declined the transaction in your wallet. Nothing was sent.";
      try {
        if (found.std) {
          const inputs = unsigned.map((t) => ({ account, transaction: Buffer.from(t, "base64") }));
          const outs = await found.std.features!["solana:signTransaction"].signTransaction(...inputs);
          return outs.map((o: { signedTransaction: Uint8Array }) => b64(new Uint8Array(o.signedTransaction)));
        }
        const p = found.p!;
        const txs = unsigned.map((t) => VersionedTransaction.deserialize(Buffer.from(t, "base64")));
        let signed: VersionedTransaction[];
        if (txs.length === 1) signed = [await p.signTransaction(txs[0])];
        else if (p.signAllTransactions) signed = await p.signAllTransactions(txs);
        else { signed = []; for (const t of txs) signed.push(await p.signTransaction(t)); }
        return signed.map((t) => b64(t.serialize()));
      } catch (e) { return declined(e, what); }
    },
  };
}
