// Tiny DOM builders shared by the picker, the system-status widgets, the
// settings sections and the brand mark. Text goes in as textContent and SVG
// is built with createElementNS — nothing is ever parsed as HTML.

/** An element with an optional class and text. */
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K, cls?: string, text?: string,
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** An SVG element with the given attributes. */
export function svgEl<K extends keyof SVGElementTagNameMap>(
  tag: K, attrs: Record<string, string>,
): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
