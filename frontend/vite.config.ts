import { defineConfig } from "vite";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = (rel: string) => fileURLToPath(new URL(rel, import.meta.url));

// Build identifier injected at compile time so the running app can show
// exactly which build it loaded (the static VERSION string never changes,
// so it can't reveal whether a stale PWA is on an old bundle). Short git
// SHA + UTC build time, with a safe fallback when git isn't available.
// execFileSync (no shell, fixed args) — no injection surface.
function buildId(): string {
  let sha = "nogit";
  try {
    sha = execFileSync("git", ["rev-parse", "--short", "HEAD"],
      { stdio: ["ignore", "pipe", "ignore"] }).toString().trim() || "nogit";
  } catch { /* not a git checkout */ }
  const ts = new Date().toISOString().replace("T", " ").slice(0, 16) + "Z";
  return `${sha} · ${ts}`;
}

export default defineConfig({
  define: {
    __BUILD_ID__: JSON.stringify(buildId()),
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8080",
      "/ws": { target: "ws://localhost:8080", ws: true },
    },
  },
  build: {
    outDir: "dist",
    target: "es2022",
    rollupOptions: {
      // HTML entry points: the terminal app (index.html), the standalone
      // rendered-Markdown viewer (viewer.html, whose heavy markdown/mermaid/
      // KaTeX deps only load on /view), and the Claude-history console view
      // (history.html, a light monospace transcript renderer on /history).
      input: {
        main: here("index.html"),
        viewer: here("viewer.html"),
        history: here("history.html"),
      },
    },
  },
});
