// Lean markdown renderer for the /history chat view. Same trusted stack as
// the /view page (markdown-it + highlight.js + DOMPurify) but without the
// file-viewer extras — no KaTeX, no Mermaid, no relative-link rewriting —
// which matches what Claude Code's TUI actually renders for prose.
import MarkdownIt from "markdown-it";
import taskLists from "markdown-it-task-lists";
import hljs from "highlight.js/lib/common";
import DOMPurify from "dompurify";
import "highlight.js/styles/github-dark.css";

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" } as Record<string, string>
  )[c]);
}

const md = new MarkdownIt({
  html: true,            // raw HTML allowed through, then DOMPurify-sanitised
  linkify: true,
  typographer: true,
  breaks: false,
  highlight: (str, lang): string => {
    if (lang && hljs.getLanguage(lang)) {
      try {
        const out = hljs.highlight(str, { language: lang }).value;
        return `<pre class="hljs"><code class="language-${lang}">${out}</code></pre>`;
      } catch { /* fall through to escaped */ }
    }
    return `<pre class="hljs"><code>${escapeHtml(str)}</code></pre>`;
  },
});
md.use(taskLists, { label: true });

// Links open in a new tab and never leak the opener.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && node.getAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

/** Render markdown source to a sanitised DOM fragment (appendChild it — no
 * innerHTML). DOMPurify does the sanitising; RETURN_DOM_FRAGMENT keeps the
 * result off any HTML-string sink. */
export function renderMarkdown(source: string): DocumentFragment {
  return DOMPurify.sanitize(md.render(source), {
    USE_PROFILES: { html: true },
    RETURN_DOM_FRAGMENT: true,
  }) as unknown as DocumentFragment;
}
