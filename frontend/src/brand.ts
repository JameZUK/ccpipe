// The ccpipe mark: a four-petal "spark" (two crossed diamonds) that slowly
// turns and breathes — the same design as docs/logo.svg. Pure SVG + CSS
// (.spark* in styles.css, which also honours prefers-reduced-motion).
//
// Each petal uses the SAME path; its rotation comes from a CSS custom
// property so it can compose with the breathing scale animation (an SVG
// transform= attribute would be overridden by the CSS transform).

import { svgEl } from "./dom";

const PETAL = "M0 -34 L4 0 L0 34 L-4 0 Z";

/** The animated mark, sized by its container (`.spark` + a size class). */
export function sparkMark(extraClass = ""): HTMLElement {
  const mark = document.createElement("div");
  mark.className = `spark ${extraClass}`.trim();
  mark.setAttribute("aria-hidden", "true");
  const svg = svgEl("svg", { class: "spark__svg", viewBox: "-40 -40 80 80" });
  const spin = svgEl("g", { class: "spark__spin" });
  for (const p of ["a", "b", "c", "d"]) {
    spin.append(svgEl("path", { class: `spark__petal spark__petal--${p}`, d: PETAL }));
  }
  svg.append(spin, svgEl("circle", { class: "spark__core", cx: "0", cy: "0", r: "2.4" }));
  mark.append(svg);
  return mark;
}
