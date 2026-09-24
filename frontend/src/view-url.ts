// The one place that builds rendered-Markdown viewer URLs. Shared by the
// viewer bundle (viewer.ts, for in-doc links and the drawer) and the main
// app (file panel, docs menu) — deliberately import-free so it adds
// nothing to either bundle.

/** /view URL for *absPath*, optionally scoping the viewer's document
 *  switcher to a project *root* (the param is omitted when not given). */
export function mdViewUrl(absPath: string, root?: string): string {
  const u = `/view?path=${encodeURIComponent(absPath)}`;
  return root ? `${u}&root=${encodeURIComponent(root)}` : u;
}
