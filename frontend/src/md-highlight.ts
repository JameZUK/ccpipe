// Shared markdown-render helpers for the /view (viewer.ts) and /history
// (md-chat.ts) pages: HTML escaping + a highlight.js callback factory. Each
// page passes its OWN hljs instance — the viewer registers the full common
// set for arbitrary files, history a small curated set — so they share the
// logic without being forced onto the same language bundle.

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" } as Record<string, string>
  )[c]);
}

interface HljsLike {
  getLanguage(name: string): unknown;
  highlight(code: string, opts: { language: string }): { value: string };
}

/** Build a markdown-it `highlight` callback for the given hljs instance.
 *  `mermaid: true` leaves ```mermaid fences tagged for post-render diagram
 *  rendering (the viewer); history doesn't render diagrams. */
export function makeHighlight(hljs: HljsLike, opts: { mermaid?: boolean } = {}) {
  return (str: string, lang: string): string => {
    if (opts.mermaid && lang === "mermaid") {
      return `<pre class="mermaid">${escapeHtml(str)}</pre>`;
    }
    if (lang && hljs.getLanguage(lang)) {
      try {
        const out = hljs.highlight(str, { language: lang }).value;
        return `<pre class="hljs"><code class="language-${lang}">${out}</code></pre>`;
      } catch { /* fall through to escaped */ }
    }
    return `<pre class="hljs"><code>${escapeHtml(str)}</code></pre>`;
  };
}
