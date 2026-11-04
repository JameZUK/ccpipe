// The code view shared by the file viewer (read-only) and the editor:
// CodeMirror 6, with the language picked from the file's name (or its #!
// line), colours matching the app, and indentation that follows the
// file: tabs where it uses tabs, its own number of spaces where it uses
// spaces.
//
// Loaded on demand (file-viewer.ts and file-panel.ts import() it), so
// none of this is in the main bundle; each language is a chunk of its
// own, fetched the first time a file of that kind is opened.
//
// Line endings and a UTF-8 byte-order mark are kept as the file had
// them: CodeMirror works in "\n" lines, so a CRLF file is opened with
// "\r\n" as its line separator, and the BOM is taken off for editing and
// put back by text().

import {
  Compartment, EditorSelection, EditorState, countColumn, type ChangeSpec, type Extension, type Text,
} from "@codemirror/state";
import {
  EditorView, highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars,
  highlightWhitespace, keymap, lineNumbers, type KeyBinding,
} from "@codemirror/view";
import {
  HighlightStyle, LanguageDescription, LanguageSupport, StreamLanguage, bracketMatching,
  foldGutter, foldKeymap, getIndentUnit, indentOnInput, indentString, indentUnit, syntaxHighlighting,
  type StreamParser,
} from "@codemirror/language";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { highlightSelectionMatches, openSearchPanel, search, searchKeymap } from "@codemirror/search";
import { languages } from "@codemirror/language-data";
import { tags as t } from "@lezer/highlight";

// ─── Preferences (this device) ───────────────────────────────────────────

const LS_PREFS = "ccpipe.codePrefs";

export interface CodePrefs {
  /** Columns a tab character takes up. */
  tabWidth: number;
  /** Wrap long lines. */
  wrap: boolean;
  /** Show spaces as dots and tabs as arrows. */
  whitespace: boolean;
  /** What Tab inserts in a file with no indentation to go by. */
  indent: IndentStyle;
}

const DEFAULT_PREFS: CodePrefs = { tabWidth: 4, wrap: false, whitespace: false, indent: { tabs: false, size: 4 } };
// On a phone-sized screen long lines wrap unless you turn it off: scrolling
// sideways to follow the cursor there loses your place.
const narrow = (): boolean => {
  try { return matchMedia("(max-width: 600px)").matches; } catch { return false; }
};
export const TAB_WIDTHS = [2, 3, 4, 8];
export const INDENT_SIZES = [2, 3, 4, 8];

export function loadPrefs(): CodePrefs {
  try {
    const v = JSON.parse(localStorage.getItem(LS_PREFS) ?? "null");
    if (v && typeof v === "object") {
      const p = { ...DEFAULT_PREFS, wrap: narrow() };
      if (TAB_WIDTHS.includes(v.tabWidth)) p.tabWidth = v.tabWidth;
      if (typeof v.wrap === "boolean") p.wrap = v.wrap;
      if (typeof v.whitespace === "boolean") p.whitespace = v.whitespace;
      if (v.indent && typeof v.indent.tabs === "boolean" && INDENT_SIZES.includes(v.indent.size)) {
        p.indent = { tabs: v.indent.tabs, size: v.indent.size };
      }
      return p;
    }
  } catch { /* private mode, bad JSON: defaults */ }
  return { ...DEFAULT_PREFS, wrap: narrow() };
}

export function savePrefs(change: Partial<CodePrefs>): CodePrefs {
  const p = { ...loadPrefs(), ...change };
  try { localStorage.setItem(LS_PREFS, JSON.stringify(p)); } catch { /* not kept */ }
  return p;
}

// ─── Indentation ─────────────────────────────────────────────────────────

export interface IndentStyle {
  tabs: boolean;
  /** Spaces per level (when not tabs). */
  size: number;
}

export function indentLabel(s: IndentStyle): string {
  return s.tabs ? "tabs" : `${s.size} spaces`;
}

export function sameIndent(a: IndentStyle, b: IndentStyle): boolean {
  return a.tabs === b.tabs && (a.tabs || a.size === b.size);
}

/** How *text* is indented, judged from its first few thousand lines, or
 * null if it isn't (or too little to tell). Spaces: the commonest step
 * between one line's indent and the next deeper one. */
export function detectIndent(text: string): IndentStyle | null {
  let tabLines = 0, spaceLines = 0;
  const steps = new Map<number, number>();
  let prev = 0;
  let start = 0;
  for (let n = 0; n < 5000 && start < text.length; n++) {
    let end = text.indexOf("\n", start);
    if (end < 0) end = text.length;
    const line = text.slice(start, end).replace(/\r$/, "");
    start = end + 1;
    if (!line.trim()) continue;
    if (line[0] === "\t") { tabLines++; prev = 0; continue; }
    const lead = line.length - line.trimStart().length;
    // " * text" inside a block comment isn't indentation.
    if (lead > 0 && /^\s*\*/.test(line) && lead % 2 === 1) continue;
    if (lead > 0 && line[lead - 1] === " ") spaceLines++;
    if (lead > prev) {
      const d = lead - prev;
      if (d <= 8) steps.set(d, (steps.get(d) ?? 0) + 1);
    }
    prev = lead;
  }
  if (tabLines === 0 && spaceLines === 0) return null;
  if (tabLines >= spaceLines) return { tabs: true, size: 4 };
  let best = 0, bestCount = 0;
  for (const [d, c] of steps) {
    if (c > bestCount || (c === bestCount && d < best)) { best = d; bestCount = c; }
  }
  return { tabs: false, size: best || 4 };
}

/** The leading whitespace of every line, re-made in style *to*. *from*
 * is how the file is indented now; *tabWidth* is where its tabs stop.
 * Whitespace-only lines are left alone. */
export function reindentText(text: string, from: IndentStyle, to: IndentStyle, tabWidth: number): string {
  const unit = from.tabs ? tabWidth : from.size;
  return text.replace(/^([ \t]+)(?=\S)/gm, (lead) => {
    let col = 0;
    for (const ch of lead) col = ch === "\t" ? (Math.floor(col / unit) + 1) * unit : col + 1;
    const levels = Math.floor(col / unit), rest = col % unit;
    return to.tabs ? "\t".repeat(levels) + " ".repeat(rest) : " ".repeat(levels * to.size + rest);
  });
}

// ─── Languages ───────────────────────────────────────────────────────────

function mode(name: string, exts: string[], parser: StreamParser<unknown>, filename?: RegExp): LanguageDescription {
  return LanguageDescription.of({
    name, extensions: exts, filename,
    load: async () => new LanguageSupport(StreamLanguage.define(parser)),
  });
}

/** Makefiles: comments, $(variables), targets, and the recipe lines. */
const makeMode: StreamParser<{ recipe: boolean }> = {
  name: "makefile",
  startState: () => ({ recipe: false }),
  token(stream, state) {
    if (stream.sol()) state.recipe = stream.peek() === "\t";
    if (stream.match(/^#.*/)) return "comment";
    if (stream.match(/^\$[({][^)}]*[)}]/) || stream.match(/^\$[@<^+?*%$]/)) return "variable-2";
    if (stream.sol() && !state.recipe) {
      if (stream.match(/^(?:-?include|ifeq|ifneq|ifdef|ifndef|else|endif|define|endef|export|unexport|override|vpath)\b/)) return "keyword";
      if (stream.match(/^[^\s:=#]+(?=\s*::?(?!=))/)) return "def";
      if (stream.match(/^[A-Za-z_][\w.]*(?=\s*(?:[:?+!]?=))/)) return "variableName";
    }
    if (stream.match(/^"(?:[^"\\]|\\.)*"?/) || stream.match(/^'[^']*'?/)) return "string";
    if (stream.match(/^[:?+!]?=|::?/)) return "operator";
    stream.next();
    return null;
  },
};

/** Windows batch files (.bat, .cmd). */
const BATCH_WORDS = new Set((
  "if else for in do goto call set setlocal endlocal echo exit not exist defined errorlevel equ neq lss "
  + "leq gtr geq shift pushd popd cd chdir start choice title pause cls copy xcopy del erase move mkdir md "
  + "rmdir rd ren rename type find findstr where off on nul").split(" "));
const batchMode: StreamParser<unknown> = {
  name: "batch",
  token(stream) {
    if (stream.sol()) {
      stream.eatSpace();
      if (stream.match(/^(?:@?rem\b|::).*/i)) return "comment";
      if (stream.match(/^:[^\s:]\S*/)) return "def";
    }
    if (stream.eatSpace()) return null;
    // %1, %~dp0 (an argument, with modifiers), %%~nf (a for loop's),
    // %NAME%, !NAME! (delayed).
    if (stream.match(/^%%~?[a-z]*[a-z]/i) || stream.match(/^%~[a-z]*\d/i) || stream.match(/^%[\d*]/)
        || stream.match(/^%[^%\s]+%/) || stream.match(/^![^!\s]+!/)) return "variable-2";
    if (stream.match(/^"[^"]*"?/)) return "string";
    if (stream.match(/^\d+\b/)) return "number";
    if (stream.match(/^@/)) return "meta";
    const word = stream.match(/^[\w.-]+/) as RegExpMatchArray | null;
    if (word) return BATCH_WORDS.has(word[0].toLowerCase()) ? "keyword" : null;
    if (stream.match(/^(?:==|>>|[|&<>()])/)) return "operator";
    stream.next();
    return null;
  },
};

const EXTRA: LanguageDescription[] = [
  mode("Makefile", ["mk", "mak", "make"], makeMode as StreamParser<unknown>, /^(?:GNU)?[Mm]akefile$/),
  mode("Batch", ["bat", "cmd"], batchMode),
];
const ALL = [...EXTRA, ...languages];

/** File names CodeMirror's list doesn't know, by the language they are. */
const BY_NAME: Record<string, string> = {
  ".bashrc": "Shell", ".bash_profile": "Shell", ".bash_aliases": "Shell", ".bash_logout": "Shell",
  ".zshrc": "Shell", ".zshenv": "Shell", ".zprofile": "Shell", ".profile": "Shell", ".envrc": "Shell",
  ".env": "Shell", ".xinitrc": "Shell", "pkgbuild": "Shell", "apkbuild": "Shell",
  ".gitignore": "Shell", ".dockerignore": "Shell", ".npmignore": "Shell", ".gitattributes": "Shell",
  ".gitconfig": "Properties files", ".editorconfig": "Properties files", ".npmrc": "Properties files",
  ".gitmodules": "Properties files", ".pylintrc": "Properties files", ".flake8": "Properties files",
  "dockerfile": "Dockerfile", "containerfile": "Dockerfile",
  "makefile": "Makefile", "gnumakefile": "Makefile",
  "cmakelists.txt": "CMake", "jenkinsfile": "Groovy",
  "gemfile": "Ruby", "rakefile": "Ruby", "vagrantfile": "Ruby", "brewfile": "Ruby",
  "podfile": "Ruby", "fastfile": "Ruby", "guardfile": "Ruby",
  "cargo.lock": "TOML", "poetry.lock": "TOML", "uv.lock": "TOML", "pipfile": "TOML",
  "yarn.lock": "YAML", "pnpm-lock.yaml": "YAML",
  ".babelrc": "JSON", ".eslintrc": "JSON", ".prettierrc": "JSON", ".swcrc": "JSON",
  "tsconfig.json": "JSON", "nginx.conf": "Nginx",
};

/** Extensions CodeMirror's list doesn't have (or gives to a rarer language). */
const BY_EXT: Record<string, string> = {
  zsh: "Shell", fish: "Shell", bashrc: "Shell", zshrc: "Shell", env: "Shell", command: "Shell",
  conf: "Properties files", cfg: "Properties files", cnf: "Properties files", inf: "Properties files",
  service: "Properties files", timer: "Properties files", socket: "Properties files",
  mount: "Properties files", target: "Properties files", desktop: "Properties files",
  network: "Properties files", netdev: "Properties files", container: "Properties files",
  jsonc: "JSON", json5: "JSON", webmanifest: "JSON", ipynb: "JSON", jsonl: "JSON", ndjson: "JSON",
  svelte: "HTML", astro: "HTML", cshtml: "HTML", razor: "HTML", xhtml: "HTML", ejs: "HTML",
  xaml: "XML", csproj: "XML", vbproj: "XML", fsproj: "XML", vcxproj: "XML",
  props: "XML", targets: "XML", plist: "XML", resx: "XML", nuspec: "XML", wsdl: "XML",
  rss: "XML", atom: "XML", ps1xml: "XML", kml: "XML", gpx: "XML", xslt: "XML", drawio: "XML",
  pssc: "PowerShell", psrc: "PowerShell", cdxml: "XML",
  pyi: "Python", pyz: "Python", gyp: "Python", gypi: "Python", star: "Python",
  inl: "C++", ipp: "C++", tpp: "C++", cu: "C++", cuh: "C++", metal: "C++", ixx: "C++",
  glsl: "C", vert: "C", frag: "C", hlsl: "C",
  hrl: "Erlang", fsx: "F#", fsi: "F#", sty: "LaTeX", cls: "LaTeX", bib: "LaTeX",
  asm: "Gas", S: "Gas", mdx: "Markdown", gemspec: "Ruby", rake: "Ruby", ru: "Ruby",
  mjs: "JavaScript", cjs: "JavaScript", es6: "JavaScript", gs: "JavaScript",
  m: "Objective-C", rs: "Rust", h: "C", hpp: "C++", ino: "C++",
};

/** What a #! line names. */
const BY_SHEBANG: [RegExp, string][] = [
  [/\b(?:ba|z|k|da|fi)?sh\b/, "Shell"], [/\bpython[\d.]*\b/, "Python"], [/\b(?:node|deno|bun)\b/, "JavaScript"],
  [/\bpwsh\b|\bpowershell\b/, "PowerShell"], [/\bruby\b/, "Ruby"], [/\bperl\b/, "Perl"], [/\blua\b/, "Lua"],
  [/\bphp\b/, "PHP"], [/\bRscript\b/, "R"], [/\btclsh\b/, "Tcl"], [/\bgroovy\b/, "Groovy"],
  [/\bjulia\b/, "Julia"], [/\bmake\b/, "Makefile"],
];

function byName(name: string): LanguageDescription | null {
  return LanguageDescription.matchLanguageName(ALL, name, false);
}

/** The language for a file called *name* (no path needed), or null for
 * plain text. *firstLine* lets a script without an extension be known by
 * its #! line. */
export function languageFor(name: string, firstLine = ""): LanguageDescription | null {
  const base = name.slice(name.lastIndexOf("/") + 1);
  const lower = base.toLowerCase();
  const named = BY_NAME[lower]
    ?? (lower.startsWith("dockerfile.") || lower.endsWith(".dockerfile") ? "Dockerfile" : undefined)
    ?? (lower.startsWith(".env.") ? "Shell" : undefined);
  if (named) return byName(named);
  for (const d of ALL) if (d.filename?.test(base)) return d;
  const dot = base.lastIndexOf(".");
  if (dot > 0 || (dot === 0 && base.length > 1)) {
    const raw = base.slice(dot + 1);
    const ext = raw.toLowerCase();
    // "config.h.in", "Makefile.in": the file before the template suffix.
    if (ext === "in" || ext === "template" || ext === "tmpl" || ext === "dist" || ext === "example" || ext === "sample") {
      const inner = languageFor(base.slice(0, dot), firstLine);
      if (inner) return inner;
    }
    const mapped = BY_EXT[raw] ?? BY_EXT[ext];
    if (mapped) return byName(mapped);
    for (const d of ALL) if (d.extensions.includes(raw)) return d;
    for (const d of ALL) if (d.extensions.some((e) => e.toLowerCase() === ext)) return d;
  }
  if (firstLine.startsWith("#!")) {
    for (const [re, lang] of BY_SHEBANG) if (re.test(firstLine)) return byName(lang);
  }
  return null;
}

async function loadLanguage(desc: LanguageDescription): Promise<Extension> {
  if (desc.name === "Markdown") {
    // Code blocks inside a Markdown file get their own language's colours.
    const { markdown } = await import("@codemirror/lang-markdown");
    return markdown({ codeLanguages: ALL });
  }
  return desc.support ?? desc.load();
}

// ─── Look ────────────────────────────────────────────────────────────────
// Colours as in the Markdown viewer's code blocks (highlight.js's GitHub
// Dark), so a file looks the same in both.

const highlightStyle = HighlightStyle.define([
  { tag: [t.keyword, t.controlKeyword, t.operatorKeyword, t.definitionKeyword, t.moduleKeyword, t.modifier, t.self], color: "#ff7b72" },
  { tag: [t.function(t.variableName), t.function(t.propertyName), t.className, t.definition(t.typeName), t.macroName], color: "#d2a8ff" },
  { tag: [t.number, t.bool, t.null, t.atom, t.constant(t.variableName), t.attributeName, t.propertyName, t.labelName, t.meta, t.processingInstruction, t.unit], color: "#79c0ff" },
  { tag: [t.string, t.special(t.string), t.regexp, t.character, t.docString, t.url], color: "#a5d6ff" },
  { tag: [t.standard(t.variableName), t.special(t.variableName), t.typeName, t.namespace, t.escape, t.annotation], color: "#ffa657" },
  { tag: [t.comment, t.lineComment, t.blockComment, t.docComment], color: "#8b949e" },
  { tag: [t.tagName, t.quote, t.angleBracket], color: "#7ee787" },
  { tag: t.definition(t.variableName), color: "#e6edf3" },
  { tag: t.heading, color: "#79c0ff", fontWeight: "bold" },
  { tag: t.strong, fontWeight: "bold" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: t.strikethrough, textDecoration: "line-through" },
  { tag: t.link, color: "#a5d6ff", textDecoration: "underline" },
  { tag: t.inserted, color: "#aff5b4", backgroundColor: "rgba(46, 160, 67, 0.15)" },
  { tag: t.deleted, color: "#ffa198", backgroundColor: "rgba(248, 81, 73, 0.15)" },
  { tag: t.changed, color: "#ffdfb6" },
  { tag: t.invalid, color: "#f85149" },
]);

const theme = EditorView.theme({
  "&": { height: "100%", color: "var(--fg)", backgroundColor: "var(--bg)", fontSize: "var(--t-sm)" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--font-mono)", lineHeight: "1.5", overscrollBehavior: "contain" },
  ".cm-content": {
    caretColor: "var(--accent)",
    padding: "var(--s-3) 0 calc(env(safe-area-inset-bottom, 0px) + var(--s-4))",
  },
  ".cm-line": { padding: "0 var(--s-4) 0 var(--s-3)" },
  ".cm-gutters": { backgroundColor: "var(--bg-elev)", color: "var(--fg-faint)", borderRight: "1px solid var(--border)" },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 var(--s-2) 0 var(--s-3)" },
  ".cm-activeLine": { backgroundColor: "rgba(239, 231, 211, 0.04)" },
  ".cm-activeLineGutter": { backgroundColor: "var(--bg-hover)", color: "var(--fg-dim)" },
  ".cm-foldGutter .cm-gutterElement": { padding: "0 4px", cursor: "pointer" },
  ".cm-foldPlaceholder": { backgroundColor: "var(--bg-hover)", border: "1px solid var(--border)", color: "var(--fg-muted)" },
  ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": { backgroundColor: "rgba(245, 165, 36, 0.22)", outline: "1px solid rgba(245, 165, 36, 0.45)" },
  ".cm-nonmatchingBracket": { color: "var(--critical)" },
  ".cm-selectionMatch": { backgroundColor: "rgba(245, 165, 36, 0.14)" },
  ".cm-searchMatch": { backgroundColor: "rgba(245, 165, 36, 0.25)", outline: "1px solid rgba(245, 165, 36, 0.5)" },
  ".cm-searchMatch.cm-searchMatch-selected": { backgroundColor: "rgba(245, 165, 36, 0.55)" },
  ".cm-highlightSpace": { backgroundImage: "radial-gradient(circle at 50% 55%, rgba(169, 160, 142, 0.55) 11%, transparent 5%)" },
  ".cm-highlightTab": {
    backgroundImage: `url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="200" height="20"><path stroke="%23a9a08e" stroke-opacity="0.6" stroke-width="1" fill="none" d="M1 10H196L190 5M190 15L196 10"/></svg>')`,
  },
  ".cm-specialChar": { color: "var(--critical)" },
  ".cm-panels": { backgroundColor: "var(--bg-elev)", color: "var(--fg)" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--border)" },
  ".cm-panels.cm-panels-bottom": { borderTop: "1px solid var(--border)" },
  ".cm-panel.cm-search": { padding: "var(--s-2) var(--s-3)", fontFamily: "var(--font-mono)", fontSize: "var(--t-xs)" },
  ".cm-panel.cm-search label": { display: "inline-flex", alignItems: "center", gap: "4px", marginRight: "var(--s-2)" },
  ".cm-panel.cm-search [name=close]": { color: "var(--fg-muted)", fontSize: "1.2rem", padding: "0 6px" },
  ".cm-textfield": {
    backgroundColor: "var(--bg-input)", color: "var(--fg)", border: "1px solid var(--border-strong)",
    borderRadius: "var(--radius-sm)", padding: "4px 6px", font: "inherit", fontSize: "max(16px, var(--t-sm))",
  },
  ".cm-textfield:focus": { outline: "none", borderColor: "var(--accent)" },
  ".cm-button": {
    backgroundImage: "none", backgroundColor: "var(--bg-input)", color: "var(--fg)",
    border: "1px solid var(--border-strong)", borderRadius: "var(--radius-sm)", padding: "4px 8px", font: "inherit",
  },
  ".cm-button:active": { backgroundImage: "none", backgroundColor: "var(--bg-hover)" },
  ".cm-tooltip": { backgroundColor: "var(--bg-elev)", border: "1px solid var(--border)", color: "var(--fg)" },
}, { dark: true });

// ─── The view ────────────────────────────────────────────────────────────

export interface CodeViewOptions {
  parent: HTMLElement;
  text: string;
  /** The file's name, for its language. */
  name: string;
  readOnly?: boolean;
  /** Called after each edit. */
  onChange?: () => void;
}

export interface CodeView {
  view: EditorView;
  /** The language's name once known (null: plain text). */
  language: Promise<string | null>;
  /** How the file is indented (null: it isn't); what Tab inserts follows it. */
  detected: IndentStyle | null;
  /** What Tab inserts now. */
  indent: IndentStyle;
  lineEnding: "LF" | "CRLF";
  bom: boolean;
  /** The text as it is to be saved: its own line endings and BOM. */
  text(): string;
  /** Changed since it was opened (or markSaved). */
  modified(): boolean;
  markSaved(): void;
  /** Lines, not counting the empty one after a final newline. */
  lines(): number;
  setWrap(on: boolean): void;
  setWhitespace(on: boolean): void;
  setTabWidth(n: number): void;
  setIndent(s: IndentStyle): void;
  /** Re-indent the whole file in style *to* (one undo step). */
  reindent(to: IndentStyle): void;
  indentMore(): void;
  indentLess(): void;
  openSearch(): void;
  focus(): void;
  destroy(): void;
}

/** Indent (dir 1) or outdent (-1) the lines under the selection to the
 * next level, re-making each line's indentation in the chosen style: a
 * tab-indented line indented with "4 spaces" chosen becomes spaces, not
 * spaces then a tab. Blank lines in a multi-line selection are left. */
function changeIndent(view: EditorView, dir: 1 | -1): boolean {
  const { state } = view;
  if (state.readOnly) return false;
  const unit = getIndentUnit(state);
  const newLead = new Map<number, string>();
  const changes: ChangeSpec[] = [];
  for (const r of state.selection.ranges) {
    const first = state.doc.lineAt(r.from).number;
    let last = state.doc.lineAt(r.to).number;
    // A selection ending at the start of a line doesn't take that line.
    if (last > first && state.doc.line(last).from === r.to) last--;
    for (let n = first; n <= last; n++) {
      if (newLead.has(n)) continue;
      const line = state.doc.line(n);
      if (last > first && !line.text.trim()) continue;
      const lead = (line.text.match(/^[ \t]*/) ?? [""])[0];
      const col = countColumn(lead, state.tabSize);
      const next = dir > 0 ? (Math.floor(col / unit) + 1) * unit : Math.max(0, Math.ceil(col / unit) - 1) * unit;
      const insert = indentString(state, next);
      newLead.set(n, insert);
      if (insert !== lead) changes.push({ from: line.from, to: line.from + lead.length, insert });
    }
  }
  if (!changes.length) return true;
  const set = state.changes(changes);
  // A cursor in the old indentation goes to the end of the new one (or
  // stays at the line's start); anything after it moves with the text.
  const place = (pos: number): number => {
    const line = state.doc.lineAt(pos);
    const lead = newLead.get(line.number);
    if (lead === undefined || pos === line.from) return set.mapPos(pos, -1);
    const oldLead = (line.text.match(/^[ \t]*/) ?? [""])[0];
    if (pos - line.from <= oldLead.length) return set.mapPos(line.from, -1) + lead.length;
    return set.mapPos(pos, 1);
  };
  view.dispatch({
    changes: set,
    selection: EditorSelection.create(
      state.selection.ranges.map((r) => EditorSelection.range(place(r.anchor), place(r.head))),
      state.selection.mainIndex),
    scrollIntoView: true,
    userEvent: dir > 0 ? "input.indent" : "delete.dedent",
  });
  return true;
}
const indentMore = (view: EditorView): boolean => changeIndent(view, 1);
const indentLess = (view: EditorView): boolean => changeIndent(view, -1);

/** Tab: in leading whitespace or over a selection, indent the line(s) a
 * level; elsewhere, insert a tab or spaces to the next stop. */
function tabKey(view: EditorView): boolean {
  const { state } = view;
  if (state.readOnly) return false;
  const unit = state.facet(indentUnit);
  const sel = state.selection;
  if (sel.ranges.some((r) => !r.empty)) return indentMore(view);
  const atLead = sel.ranges.every((r) => {
    const line = state.doc.lineAt(r.head);
    return /^[ \t]*$/.test(line.text.slice(0, r.head - line.from)) && /\S/.test(line.text);
  });
  if (atLead) return indentMore(view);
  view.dispatch(state.changeByRange((r) => {
    let insert = unit;
    if (unit !== "\t") {
      const line = state.doc.lineAt(r.head);
      let col = 0;
      for (const ch of line.text.slice(0, r.head - line.from)) col = ch === "\t" ? col + state.tabSize - (col % state.tabSize) : col + 1;
      insert = " ".repeat(unit.length - (col % unit.length));
    }
    return { changes: { from: r.from, to: r.to, insert }, range: EditorSelection.cursor(r.from + insert.length) };
  }), { scrollIntoView: true, userEvent: "input" });
  return true;
}

const tabKeys: KeyBinding[] = [{ key: "Tab", run: tabKey, shift: indentLess }];

export function createCodeView(o: CodeViewOptions): CodeView {
  const prefs = loadPrefs();
  let text = o.text;
  const bom = text.startsWith("\ufeff");
  if (bom) text = text.slice(1);
  const crlf = text.includes("\r\n") && text.split("\r\n").length - 1 >= (text.split("\n").length - 1) / 2;
  const detected = detectIndent(text);
  let indent = detected ?? prefs.indent;
  const firstLine = text.slice(0, Math.min(text.length, 200)).split("\n")[0];

  const cWrap = new Compartment(), cSpace = new Compartment(), cTab = new Compartment();
  const cUnit = new Compartment(), cLang = new Compartment();
  const unitOf = (s: IndentStyle): Extension => indentUnit.of(s.tabs ? "\t" : " ".repeat(s.size));

  const extensions: Extension[] = [
    lineNumbers(),
    foldGutter({ markerDOM: (open) => {
      const s = document.createElement("span");
      s.textContent = open ? "▾" : "▸";
      return s;
    } }),
    highlightSpecialChars(),
    syntaxHighlighting(highlightStyle, { fallback: true }),
    bracketMatching(),
    highlightSelectionMatches(),
    search({ top: true }),
    theme,
    cWrap.of(prefs.wrap ? EditorView.lineWrapping : []),
    cSpace.of(prefs.whitespace ? highlightWhitespace() : []),
    cTab.of(EditorState.tabSize.of(prefs.tabWidth)),
    cUnit.of(unitOf(indent)),
    cLang.of([]),
    EditorView.contentAttributes.of({ autocapitalize: "off", autocorrect: "off", spellcheck: "false", translate: "no" }),
  ];
  if (crlf) extensions.push(EditorState.lineSeparator.of("\r\n"));
  if (o.readOnly) {
    // Not editable (no keyboard on a phone), but focusable, so its search
    // works on all of the file rather than only the lines on screen.
    extensions.push(
      EditorState.readOnly.of(true), EditorView.editable.of(false),
      EditorView.contentAttributes.of({ tabindex: "0" }),
      keymap.of([...searchKeymap, ...foldKeymap]),
    );
  } else {
    extensions.push(
      history(), indentOnInput(), highlightActiveLine(), highlightActiveLineGutter(),
      keymap.of([...tabKeys, ...searchKeymap, ...historyKeymap, ...foldKeymap, ...defaultKeymap]),
    );
    if (o.onChange) {
      const cb = o.onChange;
      extensions.push(EditorView.updateListener.of((u) => { if (u.docChanged) cb(); }));
    }
  }

  const view = new EditorView({
    parent: o.parent,
    state: EditorState.create({ doc: text, extensions }),
  });

  const desc = languageFor(o.name, firstLine);
  let destroyed = false;
  const language = desc
    ? loadLanguage(desc).then((ext) => {
      if (!destroyed) view.dispatch({ effects: cLang.reconfigure(ext) });
      return desc.name;
    }).catch(() => null)
    : Promise.resolve(null);

  const docText = (): Text => view.state.doc;
  let savedDoc = view.state.doc;
  const cv: CodeView = {
    view, language, detected, lineEnding: crlf ? "CRLF" : "LF", bom,
    get indent() { return indent; },
    set indent(s: IndentStyle) { cv.setIndent(s); },
    text: () => (bom ? "\ufeff" : "") + view.state.sliceDoc(),
    modified: () => !view.state.doc.eq(savedDoc),
    markSaved: () => { savedDoc = view.state.doc; },
    lines: () => {
      const d = docText();
      return d.lines > 1 && d.line(d.lines).length === 0 ? d.lines - 1 : d.lines;
    },
    setWrap: (on) => view.dispatch({ effects: cWrap.reconfigure(on ? EditorView.lineWrapping : []) }),
    setWhitespace: (on) => view.dispatch({ effects: cSpace.reconfigure(on ? highlightWhitespace() : []) }),
    setTabWidth: (n) => view.dispatch({ effects: cTab.reconfigure(EditorState.tabSize.of(n)) }),
    setIndent: (s) => {
      indent = s;
      view.dispatch({ effects: cUnit.reconfigure(unitOf(s)) });
    },
    reindent: (to) => {
      const now = view.state.sliceDoc();
      const from = detectIndent(now) ?? indent;
      const next = reindentText(now, from, to, view.state.tabSize);
      if (next !== now) {
        // Line by line, so the cursor and undo history stay sensible.
        const changes = [];
        const a = now.split(view.state.lineBreak), b = next.split(view.state.lineBreak);
        let pos = 0;
        for (let i = 0; i < a.length; i++) {
          if (a[i] !== b[i]) {
            const lead = a[i].length - a[i].trimStart().length;
            const newLead = b[i].length - b[i].trimStart().length;
            changes.push({ from: pos, to: pos + lead, insert: b[i].slice(0, newLead) });
          }
          pos += a[i].length + 1;   // a line break is one position, even "\r\n"
        }
        view.dispatch({ changes, userEvent: "input.reindent" });
      }
      cv.setIndent(to);
    },
    indentMore: () => { indentMore(view); },
    indentLess: () => { indentLess(view); },
    openSearch: () => { openSearchPanel(view); },
    focus: () => view.focus(),
    destroy: () => { destroyed = true; view.destroy(); },
  };
  return cv;
}
