// What rendered markdown may NOT carry, on top of DOMPurify's defaults
// (shared by /history and /view). The text comes from transcripts and
// files on disk, so from anyone whose words Claude read: a web page it
// quoted, a cloned repo's README. Script is already out (DOMPurify, and
// the page's CSP); this keeps the text from redrawing the page around it:
//
//   - no <style>, and inline style= only for a short list of properties
//     (table alignment and what KaTeX sizes its boxes with), so nothing
//     can cover the page, hide the tool blocks or paint a fake form;
//   - no form controls, except the disabled checkbox of a task list;
//   - no data-* attributes, which the pages keep their own state in.
import DOMPurify from "dompurify";

/** Tags dropped whole. */
const FORBID_TAGS = ["style", "form", "button", "textarea", "select", "option", "dialog", "base", "link", "meta"];

/** CSS properties an inline style may set: table alignment, and what KaTeX
 *  positions its boxes with. Lengths are capped (see `lengthOk`), so a
 *  box can nudge itself within its line but not stretch or slide over the
 *  rest of the page; "position" may only be "relative"; no value may load
 *  anything (no url()). */
const OFFSET_PROPS = new Set([
  "top", "bottom", "left", "right",
  "margin", "margin-left", "margin-right", "margin-top", "margin-bottom",
  "padding", "padding-left", "padding-right", "padding-top", "padding-bottom",
  "vertical-align",
]);
const SIZE_PROPS = new Set([
  "width", "min-width", "max-width", "height", "min-height", "font-size",
  "border-width", "border-top-width", "border-bottom-width", "border-left-width", "border-right-width",
]);
const WORD_PROPS: Record<string, RegExp> = {
  "text-align": /^(left|right|center|justify|start|end)$/,
  "vertical-align": /^(baseline|top|middle|bottom|sub|super|text-top|text-bottom)$/,
  "position": /^relative$/,
  "display": /^(inline|inline-block|block|none|table|table-cell)$/,
  "font-style": /^(normal|italic|oblique)$/,
  "font-weight": /^(normal|bold|[1-9]00)$/,
  "border-style": /^(none|solid|dashed|dotted|double)$/,
  "border-top-style": /^(none|solid|dashed|dotted|double)$/,
  "border-bottom-style": /^(none|solid|dashed|dotted|double)$/,
  "border-left-style": /^(none|solid|dashed|dotted|double)$/,
  "border-right-style": /^(none|solid|dashed|dotted|double)$/,
};
const COLOR_PROPS = new Set(["color", "background-color", "border-color",
  "border-top-color", "border-bottom-color", "border-left-color", "border-right-color"]);
const COLOR = /^(#[0-9a-f]{3,8}|[a-z]{3,20}|rgba?\([\d\s.,%/]{1,40}\))$/;
const LENGTH = /^(-?)(\d{1,5}(?:\.\d{1,6})?|\.\d{1,6})(em|rem|ex|px|pt|%)?$/;

/** One or more lengths ("0 -0.02em"), each small enough for its unit. */
function lengthOk(value: string, offsets: boolean): boolean {
  const parts = value.split(/\s+/);
  if (parts.length > 4) return false;
  return parts.every((part) => {
    const m = part.match(LENGTH);
    if (!m) return false;
    const n = Number(m[2]);
    if (m[1] && !offsets) return false;
    const unit = m[3] ?? "";
    if (unit === "") return n === 0;
    if (unit === "%") return n <= 100;
    if (unit === "px" || unit === "pt") return n <= (offsets ? 200 : 2000);
    return n <= (offsets ? 20 : 100);
  });
}

/** The declarations of an inline style that are safe to keep ("" = none). */
export function safeStyle(value: string): string {
  const kept: string[] = [];
  for (const decl of value.split(";")) {
    const at = decl.indexOf(":");
    if (at < 0) continue;
    const prop = decl.slice(0, at).trim().toLowerCase();
    const val = decl.slice(at + 1).trim().toLowerCase();
    if (!val || val.length > 60) continue;
    const ok = (WORD_PROPS[prop]?.test(val) ?? false)
      || (COLOR_PROPS.has(prop) && COLOR.test(val))
      || (OFFSET_PROPS.has(prop) && lengthOk(val, true))
      || (SIZE_PROPS.has(prop) && lengthOk(val, false));
    if (ok) kept.push(`${prop}:${val}`);
  }
  return kept.join(";");
}

let hardened = false;

/** Install the hooks (once per page) and return the options to pass to
 *  every DOMPurify.sanitize() of rendered markdown. */
export function markdownSanitizeOptions(): { FORBID_TAGS: string[]; ALLOW_DATA_ATTR: false } {
  if (!hardened) {
    hardened = true;
    DOMPurify.addHook("uponSanitizeAttribute", (_node, data) => {
      if (data.attrName !== "style") return;
      const style = safeStyle(data.attrValue);
      if (style) data.attrValue = style;
      else data.keepAttr = false;
    });
    DOMPurify.addHook("uponSanitizeElement", (node, data) => {
      if (data.tagName !== "input") return;
      const el = node as Element;
      if ((el.getAttribute("type") ?? "").toLowerCase() !== "checkbox") {
        el.parentNode?.removeChild(el);
        return;
      }
      // A task list's box: shown, never editable, never part of a form.
      el.setAttribute("disabled", "");
      for (const name of ["name", "form", "formaction", "autofocus", "value"]) el.removeAttribute(name);
    });
  }
  return { FORBID_TAGS, ALLOW_DATA_ATTR: false };
}
