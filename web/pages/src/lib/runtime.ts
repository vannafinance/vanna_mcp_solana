/**
 * What the server put on the page (`<div id="root" data-…>`, see mcp_server/web/routes.py). Public values only.
 */
export interface PageConfig {
  page: "connect" | "plan";
  /** The host this page must be on; the page warns when it is opened anywhere else. */
  site: string;
  /** The network the plan acts on, in words (e.g. "Test network (test funds only)"). */
  network: string;
  /** A test network (the fork): test funds only. */
  test: boolean;
  /** The website's Dynamic environment id: the email / social wallet. Empty = only extension wallets. */
  dynamicEnv: string;
  /** This server's network (Dynamic reads the Solana chain from it; transaction links point at it), or empty. */
  dynamicRpc: string;
}

let cached: PageConfig | null = null;

export function pageConfig(): PageConfig {
  if (cached) return cached;
  const d = document.getElementById("root")?.dataset ?? {};
  cached = {
    page: d.page === "plan" ? "plan" : "connect",
    site: d.site ?? "",
    network: d.network ?? "",
    test: d.test === "1",
    dynamicEnv: d.dynamicEnv ?? "",
    dynamicRpc: d.rpc ?? "",
  };
  return cached;
}

/** A Solana transaction on an explorer: the fork through the explorer's custom-RPC view, else the cluster itself. */
export function explorerTx(signature: string): string {
  const c = pageConfig();
  const tx = `https://explorer.solana.com/tx/${encodeURIComponent(signature)}`;
  if (c.test) return c.dynamicRpc ? `${tx}?cluster=custom&customUrl=${encodeURIComponent(c.dynamicRpc)}` : `${tx}?cluster=devnet`;
  return tx;
}

export const short = (s: string, head = 4, tail = 4) => (s.length > head + tail + 1 ? `${s.slice(0, head)}…${s.slice(-tail)}` : s);
