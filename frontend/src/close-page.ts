// "Close" for the standalone pages (/view, /history), which open in their
// own window. Run as an installed app there's no tab strip or back button,
// so each page needs its own way back to the main ccpipe screen.
//
// window.close() works when the browser considers the window
// script-closable (opened via window.open, with a single history entry —
// the viewer's drawer navigates with location.replace to keep it that
// way). Where it's refused, fall back to the main app, which reopens the
// last session on its own.

export function closeToApp(): void {
  window.close();
  window.setTimeout(() => { if (!window.closed) location.replace("/"); }, 250);
}

/** Wire the button with id *id* (if present) to closeToApp. */
export function bindCloseButton(id: string): void {
  document.getElementById(id)?.addEventListener("click", closeToApp);
}
