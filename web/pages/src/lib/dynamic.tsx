/**
 * The Dynamic (dynamic.xyz) part of the email / social wallet, loaded on its own after the page shows (it is large).
 *
 * The same Dynamic environment and settings as the website (`packages/shared/dynamic/dynamic-session.tsx` and
 * `apps/solana/contexts/dynamic-solana.tsx` in Vanna_Finance_Backend) and as the Stellar MCP's pages
 * (`sign-service/web/lib/email-wallet.tsx`): the same email, Google, X or GitHub login opens the same Solana wallet.
 * Dynamic's own modal never opens; the pages' forms drive it. Dynamic only SIGNS: the server builds every message and
 * transaction, checks every signature itself and sends every transaction.
 */
import { Buffer } from "buffer";
import { useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import {
  DynamicContextProvider,
  useConnectWithOtp,
  useDynamicContext,
  useDynamicWaas,
  useIsLoggedIn,
  useSocialAccounts,
  useSwitchWallet,
  useUserWallets,
} from "@dynamic-labs/sdk-react-core";
import { SolanaWalletConnectors } from "@dynamic-labs/solana";
import { StellarWalletConnectors } from "@dynamic-labs/stellar";
import { VersionedTransaction } from "@solana/web3.js";
import { publishEmail, setDynamicControls, type SocialProvider } from "./email";

(globalThis as unknown as { Buffer: typeof Buffer }).Buffer ??= Buffer;

type Wallet = ReturnType<typeof useUserWallets>[number];
type SolanaSigner = {
  signTransaction: (tx: VersionedTransaction) => Promise<VersionedTransaction>;
  signAllTransactions: (txs: VersionedTransaction[]) => Promise<VersionedTransaction[]>;
};
type SolanaWallet = Wallet & {
  getSigner: () => Promise<SolanaSigner>;
  signMessage: (message: string) => Promise<string | undefined>;
  connector: { setActiveAccountAddress?: (address: string) => void };
};

const SOLANA_ADDRESS = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const STELLAR_ADDRESS = /^G[A-Z2-7]{55}$/;

let wallet: SolanaWallet | null = null;

/** The login's Solana wallet: the embedded one first, else any Solana wallet on the profile. */
function solanaWallet(wallets: Wallet[]): Wallet | null {
  const onChain = wallets.filter((w) => w.chain === "SOL");
  return onChain.find((w) => w.connector.isEmbeddedWallet) ?? onChain[0] ?? null;
}

/** The URL without Dynamic's one-time OAuth return parameters (dynamicOauthState, dynamicOauthCode, …). */
function withoutOauthParams(href: string): string {
  const url = new URL(href);
  for (const k of [...url.searchParams.keys()]) if (/^dynamicOauth/i.test(k)) url.searchParams.delete(k);
  return url.toString();
}

const BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function base58Decode(text: string): Uint8Array | null {
  let value = 0n;
  for (const char of text) {
    const digit = BASE58.indexOf(char);
    if (digit < 0) return null;
    value = value * 58n + BigInt(digit);
  }
  const bytes: number[] = [];
  while (value > 0n) { bytes.unshift(Number(value & 0xffn)); value >>= 8n; }
  for (const char of text) { if (char !== "1") break; bytes.unshift(0); }
  return Uint8Array.from(bytes);
}

/** The 64 signature bytes from the string the embedded wallet returns: base58 or base64, as the website reads it. */
function signatureBytes(text: string): Uint8Array {
  const b58 = base58Decode(text.trim());
  if (b58 && b58.length === 64) return b58;
  try {
    const raw = Buffer.from(text.trim(), "base64");
    if (raw.length === 64) return Uint8Array.from(raw);
  } catch { /* not base64 either */ }
  throw new Error("The email wallet returned a signature this page could not read.");
}

function signerFor(expected: string): SolanaWallet {
  if (!wallet) throw new Error("Sign in with your email first.");
  if (wallet.address !== expected) throw new Error("This email signed in to a different wallet than the one shown here.");
  // The embedded connector signs for its "active" account, which Dynamic sets only for the primary wallet.
  wallet.connector.setActiveAccountAddress?.(wallet.address);
  return wallet;
}

function Bridge() {
  const { sdkHasLoaded, handleLogOut, user, primaryWallet } = useDynamicContext();
  const loggedIn = useIsLoggedIn();
  const wallets = useUserWallets();
  const switchWallet = useSwitchWallet();
  const { createWalletAccount, dynamicWaasIsEnabled } = useDynamicWaas();
  const { connectWithEmail, verifyOneTimePassword, retryOneTimePassword } = useConnectWithOtp();
  const { signInWithSocialAccount } = useSocialAccounts();
  const creatingFor = useRef<string | null>(null);
  const switchingTo = useRef<string | null>(null);

  useEffect(() => {
    setDynamicControls({
      sendCode: async (email) => { await connectWithEmail(email); },
      verifyCode: async (code) => { await verifyOneTimePassword(code); },
      resendCode: async () => { await retryOneTimePassword(); },
      // Leaves for the provider and comes back to this same page (without an earlier sign-in's OAuth parameters).
      social: async (provider: SocialProvider) => {
        await signInWithSocialAccount(provider as never, { redirectUrl: withoutOauthParams(window.location.href) });
      },
      logout: () => handleLogOut(),
      async signMessage(text, expected) {
        const signed = await signerFor(expected).signMessage(text);
        if (typeof signed !== "string" || !signed) throw new Error("The email wallet did not return a signature.");
        return signatureBytes(signed);
      },
      async signTransactions(unsigned, expected) {
        const w = signerFor(expected);
        const txs = unsigned.map((b64) => VersionedTransaction.deserialize(Buffer.from(b64, "base64")));
        const s = await w.getSigner();
        const signed = txs.length === 1 ? [await s.signTransaction(txs[0])] : await s.signAllTransactions(txs);
        return signed.map((tx) => Buffer.from(tx.serialize()).toString("base64"));
      },
    });
    return () => setDynamicControls(null);
  }, [connectWithEmail, verifyOneTimePassword, retryOneTimePassword, signInWithSocialAccount, handleLogOut]);

  const current = loggedIn ? solanaWallet(wallets) : null;
  const userId = user?.userId ?? null;
  // Whether the login already has its embedded Solana wallet, from the verified credentials (the complete list Dynamic
  // keeps). useUserWallets() can be briefly empty while the page loads; deciding from it would create a second wallet.
  const hasEmbedded = (user?.verifiedCredentials ?? []).some(
    (c) => c.walletProvider === "embeddedWallet" && SOLANA_ADDRESS.test(c.address ?? "") && !STELLAR_ADDRESS.test(c.address ?? ""),
  );
  const needsWallet = sdkHasLoaded && loggedIn && !current && !hasEmbedded && dynamicWaasIsEnabled && !!userId;

  // A login made on Stellar has no Solana wallet yet: create it, as the website does.
  useEffect(() => {
    if (!needsWallet || creatingFor.current === userId) return;
    creatingFor.current = userId;
    createWalletAccount(["SOL" as never]).catch((error: unknown) => {
      console.warn("Dynamic: creating the Solana wallet failed", error);
      creatingFor.current = null;
    });
  }, [needsWallet, userId, createWalletAccount]);

  // The embedded signer signs for the primary wallet: make the Solana wallet primary whenever it is not.
  const walletId = current?.id ?? null;
  useEffect(() => {
    if (!walletId || primaryWallet?.id === walletId || switchingTo.current === walletId) return;
    switchingTo.current = walletId;
    switchWallet(walletId)
      .catch((error: unknown) => console.warn("Dynamic: switching to the Solana wallet failed", error))
      .finally(() => { switchingTo.current = null; });
  }, [walletId, primaryWallet?.id, switchWallet]);

  // Back from Google / X / GitHub the URL carries Dynamic's one-time OAuth state and code: take them off the address bar.
  useEffect(() => {
    if (!sdkHasLoaded) return;
    const clean = withoutOauthParams(window.location.href);
    if (clean === window.location.href) return;
    const t = setTimeout(() => window.history.replaceState(window.history.state, "", clean), loggedIn ? 0 : 5_000);
    return () => clearTimeout(t);
  }, [sdkHasLoaded, loggedIn]);

  useEffect(() => {
    wallet = (current as SolanaWallet | null) ?? null;
    const handle = user?.verifiedCredentials?.find((c) => c.oauthUsername)?.oauthUsername ?? null;
    publishEmail({
      ready: sdkHasLoaded,
      failed: false,
      loggedIn,
      creating: loggedIn && !current,
      address: current?.address ?? null,
      label: loggedIn ? (user?.email ?? handle) : null,
    });
  }, [sdkHasLoaded, loggedIn, current, user?.email, user?.verifiedCredentials]);
  return null;
}

type Network = { name?: string; rpcUrls?: string[]; privateCustomerRpcUrls?: string[] };
/** Stellar wallets on the same login use Stellar testnet, as on the website. */
const stellarNetworks = (dashboard: Network[]): Network[] => {
  const testnet = dashboard.filter((n) => `${n.name ?? ""} ${n.rpcUrls?.[0] ?? ""}`.toLowerCase().includes("testnet"));
  return testnet.length ? testnet : dashboard;
};

/** Mounts Dynamic in its own hidden root; the pages read it through `email.ts`. */
export function mountDynamic(environmentId: string, rpc: string) {
  // Every Solana network Dynamic knows points at this server's own network, as the website does.
  const solNetworks = (dashboard: Network[]): Network[] =>
    rpc ? dashboard.map((n) => ({ ...n, rpcUrls: [rpc], privateCustomerRpcUrls: [rpc] })) : dashboard;
  const host = document.createElement("div");
  host.id = "vanna-dynamic";
  document.body.appendChild(host);
  createRoot(host).render(
    <DynamicContextProvider
      theme={document.documentElement.classList.contains("dark") ? "dark" : "light"}
      settings={{
        environmentId,
        // Both connectors: "create on sign up" makes a wallet for every chain enabled in the dashboard and fails the whole
        // signup if one is missing (the website and the Stellar MCP mount both for the same reason).
        walletConnectors: [SolanaWalletConnectors, StellarWalletConnectors],
        overrides: { solNetworks: solNetworks as never, stellarNetworks: stellarNetworks as never },
        appName: "Vanna",
        initialAuthenticationMode: "connect-and-sign",
        // Email and social only: the extension wallets have their own buttons on these pages, as on the website.
        walletsFilter: () => [],
      }}
    >
      <Bridge />
    </DynamicContextProvider>,
  );
}
