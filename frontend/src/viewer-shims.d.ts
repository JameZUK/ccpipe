// Ambient declarations for Markdown-viewer plugins that don't ship their
// own TypeScript types. Kept minimal — just enough for the viewer to use
// them as markdown-it plugins. (markdown-it, markdown-it-anchor, katex,
// dompurify, highlight.js and mermaid all ship their own types.)

declare module "markdown-it-task-lists" {
  import type MarkdownIt from "markdown-it";
  const plugin: (md: MarkdownIt, opts?: Record<string, unknown>) => void;
  export default plugin;
}

declare module "markdown-it-texmath" {
  import type MarkdownIt from "markdown-it";
  const plugin: (md: MarkdownIt, opts?: Record<string, unknown>) => void;
  export default plugin;
}
