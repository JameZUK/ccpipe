// Lean markdown renderer for the /history chat view. Same trusted approach as
// /view (markdown-it + highlight.js + DOMPurify, returning a sanitised DOM
// fragment so it never touches an HTML-string sink) but built on
// highlight.js/lib/core with only the languages Claude actually emits — far
// smaller than lib/common's 37 — and no KaTeX/Mermaid (Claude Code's TUI
// doesn't render those either).
import MarkdownIt from "markdown-it";
import taskLists from "markdown-it-task-lists";
import hljs from "highlight.js/lib/core";
import DOMPurify from "dompurify";
import "highlight.js/styles/github-dark.css";
import { makeHighlight } from "./md-highlight";

import bash from "highlight.js/lib/languages/bash";
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import python from "highlight.js/lib/languages/python";
import json from "highlight.js/lib/languages/json";
import diff from "highlight.js/lib/languages/diff";
import yaml from "highlight.js/lib/languages/yaml";
import xml from "highlight.js/lib/languages/xml";
import css from "highlight.js/lib/languages/css";
import markdown from "highlight.js/lib/languages/markdown";
import sql from "highlight.js/lib/languages/sql";
import rust from "highlight.js/lib/languages/rust";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import java from "highlight.js/lib/languages/java";
import ruby from "highlight.js/lib/languages/ruby";
import php from "highlight.js/lib/languages/php";
import kotlin from "highlight.js/lib/languages/kotlin";
import swift from "highlight.js/lib/languages/swift";
import lua from "highlight.js/lib/languages/lua";
import perl from "highlight.js/lib/languages/perl";
import r from "highlight.js/lib/languages/r";
import scss from "highlight.js/lib/languages/scss";
import makefile from "highlight.js/lib/languages/makefile";
import graphql from "highlight.js/lib/languages/graphql";
import plaintext from "highlight.js/lib/languages/plaintext";

for (const [name, lang] of Object.entries({
  bash, javascript, typescript, python, json, diff, yaml, xml, css,
  markdown, sql, rust, go, ini, dockerfile,
  c, cpp, csharp, java, ruby, php, kotlin, swift, lua, perl, r,
  scss, makefile, graphql, plaintext,
})) hljs.registerLanguage(name, lang);
hljs.registerAliases(["sh", "shell", "zsh", "console"], { languageName: "bash" });
hljs.registerAliases(["js", "jsx"], { languageName: "javascript" });
hljs.registerAliases(["ts", "tsx"], { languageName: "typescript" });
hljs.registerAliases(["py"], { languageName: "python" });
hljs.registerAliases(["yml"], { languageName: "yaml" });
hljs.registerAliases(["html"], { languageName: "xml" });
hljs.registerAliases(["toml"], { languageName: "ini" });
hljs.registerAliases(["h", "hpp", "cc", "c++"], { languageName: "cpp" });
hljs.registerAliases(["rb"], { languageName: "ruby" });
hljs.registerAliases(["cs"], { languageName: "csharp" });
hljs.registerAliases(["kt"], { languageName: "kotlin" });
hljs.registerAliases(["pl"], { languageName: "perl" });
hljs.registerAliases(["text", "txt"], { languageName: "plaintext" });

const md = new MarkdownIt({
  html: true,            // raw HTML allowed through, then DOMPurify-sanitised
  linkify: true,
  typographer: true,
  breaks: false,
  highlight: makeHighlight(hljs),
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
 *  innerHTML). DOMPurify does the sanitising; RETURN_DOM_FRAGMENT keeps the
 *  result off any HTML-string sink. */
export function renderMarkdown(source: string): DocumentFragment {
  return DOMPurify.sanitize(md.render(source), {
    USE_PROFILES: { html: true },
    RETURN_DOM_FRAGMENT: true,
  }) as unknown as DocumentFragment;
}
