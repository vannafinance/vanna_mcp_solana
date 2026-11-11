/**
 * The MCP's own pages (`/connect`, `/plan/<id>`), built from the Stellar MCP's page components so both MCPs look and work
 * the same. The server renders an empty shell with the page's config on `#root` (mcp_server/web/routes.py); this renders it.
 */
import { Buffer } from "buffer";
import { createRoot } from "react-dom/client";
import { ConnectPage } from "./pages/connect";
import { PlanPage } from "./pages/plan";
import { startEmailWallet } from "./lib/email";
import { pageConfig } from "./lib/runtime";

(globalThis as unknown as { Buffer: typeof Buffer }).Buffer ??= Buffer;

// Follows the device theme, as the Stellar pages do (they set it before first paint; here the bundle is the first script).
try {
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  const apply = () => document.documentElement.classList.toggle("dark", dark.matches);
  apply();
  dark.addEventListener("change", apply);
} catch { /* light */ }

startEmailWallet();
const root = document.getElementById("root");
if (root) createRoot(root).render(pageConfig().page === "plan" ? <PlanPage /> : <ConnectPage />);
