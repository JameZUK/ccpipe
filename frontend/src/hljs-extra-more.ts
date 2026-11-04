// The rarer highlight.js languages, for code blocks on the /view page
// only (see hljs-extra.ts): registerExtraLanguages(hljs, MORE_LANGUAGES).

import type { LanguageFn } from "highlight.js";
import lang_scheme from "highlight.js/lib/languages/scheme";
import lang_julia from "highlight.js/lib/languages/julia";
import lang_matlab from "highlight.js/lib/languages/matlab";
import lang_awk from "highlight.js/lib/languages/awk";
import lang_tcl from "highlight.js/lib/languages/tcl";
import lang_nim from "highlight.js/lib/languages/nim";
import lang_crystal from "highlight.js/lib/languages/crystal";
import lang_d from "highlight.js/lib/languages/d";
import lang_elm from "highlight.js/lib/languages/elm";
import lang_llvm from "highlight.js/lib/languages/llvm";
import lang_glsl from "highlight.js/lib/languages/glsl";
import lang_x86asm from "highlight.js/lib/languages/x86asm";
import lang_armasm from "highlight.js/lib/languages/armasm";
import lang_fortran from "highlight.js/lib/languages/fortran";
import lang_delphi from "highlight.js/lib/languages/delphi";
import lang_ada from "highlight.js/lib/languages/ada";
import lang_prolog from "highlight.js/lib/languages/prolog";
import lang_coffeescript from "highlight.js/lib/languages/coffeescript";
import lang_handlebars from "highlight.js/lib/languages/handlebars";
import lang_autohotkey from "highlight.js/lib/languages/autohotkey";
import lang_applescript from "highlight.js/lib/languages/applescript";
import lang_verilog from "highlight.js/lib/languages/verilog";
import lang_vhdl from "highlight.js/lib/languages/vhdl";
import lang_less from "highlight.js/lib/languages/less";
import lang_erb from "highlight.js/lib/languages/erb";
import lang_twig from "highlight.js/lib/languages/twig";
import lang_django from "highlight.js/lib/languages/django";

export const MORE_LANGUAGES: Record<string, LanguageFn> = {
  "scheme": lang_scheme, "julia": lang_julia, "matlab": lang_matlab, "awk": lang_awk,
  "tcl": lang_tcl, "nim": lang_nim, "crystal": lang_crystal, "d": lang_d, "elm": lang_elm,
  "llvm": lang_llvm, "glsl": lang_glsl, "x86asm": lang_x86asm, "armasm": lang_armasm,
  "fortran": lang_fortran, "delphi": lang_delphi, "ada": lang_ada, "prolog": lang_prolog,
  "coffeescript": lang_coffeescript, "handlebars": lang_handlebars, "autohotkey": lang_autohotkey,
  "applescript": lang_applescript, "verilog": lang_verilog, "vhdl": lang_vhdl, "less": lang_less,
  "erb": lang_erb, "twig": lang_twig, "django": lang_django,
};
