// In-app replacements for window.prompt() / window.confirm(), which
// installed mobile PWAs suppress (they return null / false with no UI).
// Shared by the file panel (rename, new dir) and the composer's saved
// prompts (save-as name, replace confirm).

/** A small styled input dialog returning the entered text, or null if
 * cancelled. Replaces window.prompt(), which installed mobile PWAs
 * suppress (returning null with no UI), making rename / mkdir appear to
 * silently no-op. Enter confirms, Escape / backdrop / Cancel dismisses. */
export function inlinePrompt(title: string, initial = ""): Promise<string | null> {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "modal-overlay";
    const box = document.createElement("div");
    box.className = "inline-prompt";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", title);
    const label = document.createElement("label");
    label.className = "inline-prompt__label";
    label.textContent = title;
    const input = document.createElement("input");
    input.type = "text";
    input.className = "inline-prompt__input";
    input.value = initial;
    const actions = document.createElement("div");
    actions.className = "inline-prompt__actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    cancel.textContent = "Cancel";
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "btn btn--primary";
    ok.textContent = "OK";

    let done = false;
    const finish = (val: string | null): void => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey, true);
      ov.remove();
      resolve(val);
    };
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") { e.preventDefault(); finish(null); }
      else if (e.key === "Enter") { e.preventDefault(); finish(input.value); }
    }
    cancel.addEventListener("click", () => finish(null));
    ok.addEventListener("click", () => finish(input.value));
    ov.addEventListener("click", (e) => { if (e.target === ov) finish(null); });
    document.addEventListener("keydown", onKey, true);

    label.append(input);
    actions.append(cancel, ok);
    box.append(label, actions);
    ov.append(box);
    document.body.append(ov);
    setTimeout(() => { input.focus(); input.select(); }, 30);
  });
}

/** Two-button confirmation in the same style as inlinePrompt. Resolves
 *  true for the confirm button (or Enter), false otherwise. */
export function inlineConfirm(message: string, okLabel = "OK"): Promise<boolean> {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "modal-overlay";
    const box = document.createElement("div");
    box.className = "inline-prompt";
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", message);
    const text = document.createElement("p");
    text.className = "inline-prompt__label";
    text.textContent = message;
    const actions = document.createElement("div");
    actions.className = "inline-prompt__actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    cancel.textContent = "Cancel";
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "btn btn--primary";
    ok.textContent = okLabel;

    let done = false;
    const finish = (val: boolean): void => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey, true);
      ov.remove();
      resolve(val);
    };
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
      else if (e.key === "Enter") { e.preventDefault(); finish(true); }
    }
    cancel.addEventListener("click", () => finish(false));
    ok.addEventListener("click", () => finish(true));
    ov.addEventListener("click", (e) => { if (e.target === ov) finish(false); });
    document.addEventListener("keydown", onKey, true);

    actions.append(cancel, ok);
    box.append(text, actions);
    ov.append(box);
    document.body.append(ov);
    setTimeout(() => ok.focus(), 30);
  });
}
