import { siClaude, siCline, siCursor, siGithubcopilot, siGoogle, siGooglegemini, siWindsurf, siZedindustries, type SimpleIcon } from "simple-icons";

/**
 * Agents the pages can show with their own logo (the Stellar MCP's list, lib/agents.ts there). An agent registers its own
 * name, so a name alone proves nothing; the logo appears only when something the agent cannot fake agrees with it:
 * - `hosts`: the sign-in returns to one of these domains. Nobody else can receive a code there (Cursor, Antigravity, Claude).
 * - `names`: for an agent on the person's own machine, which returns to localhost (Claude Code and other CLIs and editors),
 *   the registered name, matched whole or as "<name> (…)".
 * Anything else keeps the plain initial. A brand missing from simple-icons uses the image its maker publishes (`image`, a
 * file under /static), drawn on `tile`.
 */
interface KnownAgent { label: string; icon?: SimpleIcon; image?: string; tile?: string; hosts?: string[]; names?: string[] }

const AGENTS: KnownAgent[] = [
  { label: "Cursor", icon: siCursor, hosts: ["cursor.com", "www.cursor.com"], names: ["Cursor"] },
  { label: "Antigravity", icon: siGoogle, hosts: ["antigravity.google"], names: ["Google Antigravity", "Antigravity"] },
  { label: "Claude", icon: siClaude, hosts: ["claude.ai"] },
  { label: "Claude Code", icon: siClaude, names: ["Claude Code"] },
  { label: "Gemini CLI", icon: siGooglegemini, names: ["Gemini CLI", "gemini-cli"] },
  { label: "GitHub Copilot", icon: siGithubcopilot, hosts: ["vscode.dev"], names: ["Visual Studio Code", "GitHub Copilot"] },
  { label: "Windsurf", icon: siWindsurf, names: ["Windsurf"] },
  { label: "Zed", icon: siZedindustries, names: ["Zed"] },
  { label: "Cline", icon: siCline, names: ["Cline"] },
  { label: "Codex", image: "/static/agent-codex.png", tile: "#000000", names: ["Codex", "Codex CLI"] },
];

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);

export type AgentIdentity = Pick<KnownAgent, "label" | "icon" | "image" | "tile">;
const identity = ({ label, icon, image, tile }: KnownAgent): AgentIdentity => ({ label, icon, image, tile });

/** The agent behind this sign-in, when its return address backs up who it says it is; null otherwise. */
export function identifyAgent(name: string | null | undefined, redirectHost: string | null | undefined): AgentIdentity | null {
  if (!redirectHost) return null;
  const hostname = redirectHost.replace(/:\d+$/, "").toLowerCase();
  const byHost = AGENTS.find((agent) => agent.hosts?.includes(hostname));
  if (byHost) return identity(byHost);
  if (!LOOPBACK.has(hostname) || !name) return null;
  const said = name.trim().toLowerCase();
  const byName = AGENTS.find((agent) => agent.names?.some((n) => said === n.toLowerCase() || said.startsWith(`${n.toLowerCase()} (`)));
  return byName ? identity(byName) : null;
}
