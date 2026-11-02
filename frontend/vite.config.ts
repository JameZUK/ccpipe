import { defineConfig, type Plugin } from "vite";
import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
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

const BUILD_ID = buildId();

// How many builds' hashed files stay in dist/assets.
const KEEP_BUILDS = 3;

// Self-update support (see src/update.ts):
//  • writes dist/version.json — {"build": BUILD_ID} — which open pages poll
//    to notice a deploy and reload themselves onto it;
//  • keeps the hashed files of the last KEEP_BUILDS builds instead of
//    emptying dist/ (emptyOutDir is off below). A page loaded before a
//    deploy lazily imports chunks of ITS build (terminal, settings, …);
//    with them deleted the import 404'd and the page was left blank.
//    dist/.builds.json records which files belong to which build; files of
//    older builds are pruned after each build.
function ccpipeBuildInfo(): Plugin {
  let outDir = "";
  return {
    name: "ccpipe-build-info",
    apply: "build",
    configResolved(cfg) { outDir = resolve(cfg.root, cfg.build.outDir); },
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "version.json", source: JSON.stringify({ build: BUILD_ID }) + "\n" });
    },
    writeBundle(_opts, bundle) {
      const assetsDir = join(outDir, "assets");
      if (!existsSync(assetsDir)) return;
      const ledger = join(outDir, ".builds.json");
      const mine = Object.keys(bundle).filter((f) => f.startsWith("assets/")).map((f) => f.slice("assets/".length));
      type Entry = { build: string; files: string[] };
      let builds: Entry[] = [];
      try {
        const parsed = JSON.parse(readFileSync(ledger, "utf8"));
        if (Array.isArray(parsed)) builds = parsed.filter((b) => b && typeof b.build === "string" && Array.isArray(b.files));
      } catch {
        // No ledger yet (first build with it): whatever is already there
        // belongs to the build being replaced, so keep it as one.
        const mineSet = new Set(mine);
        const earlier = readdirSync(assetsDir).filter((f) => !mineSet.has(f));
        if (earlier.length) builds = [{ build: "earlier", files: earlier }];
      }
      builds = [...builds.filter((b) => b.build !== BUILD_ID), { build: BUILD_ID, files: mine }].slice(-KEEP_BUILDS);
      writeFileSync(ledger, JSON.stringify(builds) + "\n");
      const keep = new Set(builds.flatMap((b) => b.files));
      for (const f of readdirSync(assetsDir)) {
        if (!keep.has(f)) rmSync(join(assetsDir, f), { recursive: true, force: true });
      }
    },
  };
}

export default defineConfig({
  plugins: [ccpipeBuildInfo()],
  define: {
    __BUILD_ID__: JSON.stringify(BUILD_ID),
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
    // Old builds' files are pruned by ccpipeBuildInfo, not wiped up front.
    emptyOutDir: false,
    target: "es2022",
    // Never inline fonts as data: URIs — the CSPs are `font-src 'self'`,
    // so an inlined font is silently blocked (KaTeX_Size3, under the 4 KB
    // default limit, was; big delimiters fell back to a system font).
    // Other small assets keep Vite's default inlining.
    assetsInlineLimit: (file) => (/\.(woff2?|ttf|otf|eot)$/i.test(file) ? false : undefined),
    rollupOptions: {
      // HTML entry points: the terminal app (index.html), the standalone
      // rendered-Markdown viewer (viewer.html, whose heavy markdown/mermaid/
      // KaTeX deps only load on /view), and the Claude-history console view
      // (history.html, the console-style conversation view on /history; it
      // markdown-renders prose via markdown-it + highlight.js, but only a
      // curated language set, so its chunk stays well under the viewer's).
      input: {
        main: here("index.html"),
        viewer: here("viewer.html"),
        history: here("history.html"),
      },
    },
  },
});
