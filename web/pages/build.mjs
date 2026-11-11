// Builds the MCP's pages into mcp_server/web/static/app/: app.css (Tailwind, the Stellar pages' theme) and main.js with its
// chunks (Dynamic is a chunk of its own, loaded after the page shows). Run `npm ci && npm run build` here before starting the
// server locally; the Dockerfile does the same in its Node stage.
import { build } from "esbuild";
import { execFileSync } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import zlib, { brotliCompressSync, gzipSync } from "node:zlib";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const out = path.join(here, "../../mcp_server/web/static/app");
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });

const cli = createRequire(import.meta.url).resolve("@tailwindcss/cli/package.json");
execFileSync(process.execPath, [path.join(path.dirname(cli), "dist/index.mjs"), "-i", path.join(here, "src/globals.css"),
  "-o", path.join(out, "app.css"), "--minify"], { stdio: ["ignore", "ignore", "inherit"], cwd: here });

await build({
  entryPoints: { main: path.join(here, "src/main.tsx") },
  outdir: out,
  bundle: true,
  splitting: true,
  format: "esm",
  platform: "browser",
  target: ["es2020"],
  minify: true,
  legalComments: "none",
  jsx: "automatic",
  chunkNames: "chunk-[hash]",
  define: { "process.env.NODE_ENV": '"production"', global: "globalThis" },
  logLevel: "warning",
});
// Compressed copies next to each file (the Dynamic chunk is ~5 MB raw, ~1 MB compressed); the server picks the one the
// browser accepts (mcp_server/web/routes.py `_app_static`).
for (const name of readdirSync(out)) {
  if (!/\.(js|css)$/.test(name)) continue;
  const raw = readFileSync(path.join(out, name));
  writeFileSync(path.join(out, `${name}.gz`), gzipSync(raw, { level: 9 }));
  writeFileSync(path.join(out, `${name}.br`), brotliCompressSync(raw, { params: { [zlib.constants.BROTLI_PARAM_QUALITY]: 11 } }));
}
console.log("pages built into", path.relative(process.cwd(), out));
