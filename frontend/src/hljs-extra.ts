// highlight.js languages beyond its "common" set, for code blocks in
// Markdown (the /view page and /history): PowerShell, batch files, build
// and config files, and more. The file viewer and editor colour whole
// files with CodeMirror instead (code-view.ts), which has these too.
// The rarer languages are in hljs-extra-more.ts, for /view only, so the
// /history page stays small.

import type { HLJSApi, LanguageFn } from "highlight.js";
import lang_powershell from "highlight.js/lib/languages/powershell";
import lang_dos from "highlight.js/lib/languages/dos";
import lang_dockerfile from "highlight.js/lib/languages/dockerfile";
import lang_cmake from "highlight.js/lib/languages/cmake";
import lang_nginx from "highlight.js/lib/languages/nginx";
import lang_apache from "highlight.js/lib/languages/apache";
import lang_properties from "highlight.js/lib/languages/properties";
import lang_makefile from "highlight.js/lib/languages/makefile";
import lang_haskell from "highlight.js/lib/languages/haskell";
import lang_scala from "highlight.js/lib/languages/scala";
import lang_dart from "highlight.js/lib/languages/dart";
import lang_elixir from "highlight.js/lib/languages/elixir";
import lang_erlang from "highlight.js/lib/languages/erlang";
import lang_groovy from "highlight.js/lib/languages/groovy";
import lang_gradle from "highlight.js/lib/languages/gradle";
import lang_protobuf from "highlight.js/lib/languages/protobuf";
import lang_fsharp from "highlight.js/lib/languages/fsharp";
import lang_objectivec from "highlight.js/lib/languages/objectivec";
import lang_vbnet from "highlight.js/lib/languages/vbnet";
import lang_latex from "highlight.js/lib/languages/latex";
import lang_nix from "highlight.js/lib/languages/nix";
import lang_http from "highlight.js/lib/languages/http";
import lang_vim from "highlight.js/lib/languages/vim";
import lang_clojure from "highlight.js/lib/languages/clojure";
import lang_ocaml from "highlight.js/lib/languages/ocaml";
import lang_lisp from "highlight.js/lib/languages/lisp";
import lang_graphql from "highlight.js/lib/languages/graphql";

const EXTRA: Record<string, LanguageFn> = {
  "powershell": lang_powershell, "dos": lang_dos, "dockerfile": lang_dockerfile,
  "cmake": lang_cmake, "nginx": lang_nginx, "apache": lang_apache, "properties": lang_properties,
  "makefile": lang_makefile, "haskell": lang_haskell, "scala": lang_scala, "dart": lang_dart,
  "elixir": lang_elixir, "erlang": lang_erlang, "groovy": lang_groovy, "gradle": lang_gradle,
  "protobuf": lang_protobuf, "fsharp": lang_fsharp, "objectivec": lang_objectivec,
  "vbnet": lang_vbnet, "latex": lang_latex, "nix": lang_nix, "http": lang_http, "vim": lang_vim,
  "clojure": lang_clojure, "ocaml": lang_ocaml, "lisp": lang_lisp, "graphql": lang_graphql,
};

// Names a code block might be tagged with that the languages don't list.
const ALIASES: Record<string, string[]> = {
  powershell: ["pwsh", "ps", "ps1", "psm1", "psd1"],
  dos: ["batch", "bat", "cmd"],
  dockerfile: ["containerfile"],
  ini: ["toml", "conf", "cfg", "systemd"],
  bash: ["zsh", "sh", "shell", "console", "fish"],
  cpp: ["h", "hpp", "cc", "cxx", "c++", "cuda"],
  javascript: ["js", "jsx", "mjs", "cjs"],
  typescript: ["ts", "tsx", "mts", "cts"],
  xml: ["html", "xhtml", "svg", "xaml", "csproj", "plist", "vue", "svelte"],
  json: ["jsonc", "json5", "jsonl"],
  plaintext: ["text", "txt", "log"],
};

/** Add the extra languages (and *more*, and the aliases) to *hljs*,
 * keeping any it has. */
export function registerExtraLanguages(hljs: HLJSApi, more: Record<string, LanguageFn> = {}): void {
  for (const [name, fn] of Object.entries({ ...EXTRA, ...more })) {
    if (!hljs.getLanguage(name)) hljs.registerLanguage(name, fn);
  }
  for (const [name, aliases] of Object.entries(ALIASES)) {
    if (!hljs.getLanguage(name)) continue;
    const free = aliases.filter((a) => !hljs.getLanguage(a));
    if (free.length) hljs.registerAliases(free, { languageName: name });
  }
}
