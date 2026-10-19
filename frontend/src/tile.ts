// A secondary terminal tile shown beside the main terminal (iPad / desktop).
//
// Each tile is its own WebSocket + xterm attached to another tmux session.
// Two modes:
//   - "watch": a read-only tmux client that tmux leaves out of the window
//     size calculation, so watching never resizes the session (or reflows
//     claude on your other devices). The server sizes the client to the
//     session; the tile scales the result to fit.
//   - "live":  a normal interactive attach, sized to the tile like the main
//     terminal.
// Typing into a watch tile switches it to live (the keystrokes are kept
// and sent once the live connection is up).
//
// Tiles never speak (no TTS) and have no mic; the main terminal owns both.

import { loadDisplayPrefs, onDisplayPrefsChange } from "./display-prefs";
import { ICON_EXPAND, ICON_OPEN, ICON_X } from "./icons";
import type { createTerminal as CreateTerminal } from "./terminal";
import { TerminalSocket } from "./ws";

export type TileMode = "live" | "watch";

export interface TileHooks {
  /** The tile was clicked / its terminal took focus. */
  onFocus(tile: Tile): void;
  onClose(tile: Tile): void;
  onModeChange(tile: Tile, mode: TileMode): void;
  onMaximize(tile: Tile): void;
  /** Make this session the main one (its own layout loads). */
  onOpenMain(session: string): void;
}

type TerminalApi = ReturnType<typeof CreateTerminal>;

export class Tile {
  readonly el: HTMLDivElement;
  private readonly head: HTMLDivElement;
  private readonly nameEl: HTMLSpanElement;
  private readonly dot: HTMLSpanElement;
  private readonly modeBtn: HTMLButtonElement;
  private readonly body: HTMLDivElement;
  private socket: TerminalSocket | null = null;
  private api: TerminalApi | null = null;
  private unsubPrefs: (() => void) | null = null;
  private bottomOnNextOutput = false;
  private pendingInput: string[] = [];
  private activityTimer: number | null = null;
  private gone = false;
  private disposed = false;
  focused = false;

  constructor(
    public session: string,
    public mode: TileMode,
    private readonly urlFor: (session: string, mode: TileMode) => string,
    private readonly createTerminal: typeof CreateTerminal,
    private readonly hooks: TileHooks,
  ) {
    this.el = document.createElement("div");
    this.el.className = "tile";
    this.head = document.createElement("div");
    this.head.className = "tile__head";
    this.dot = document.createElement("span");
    this.dot.className = "tile__dot";
    this.nameEl = document.createElement("span");
    this.nameEl.className = "tile__name";
    this.modeBtn = headButton("", "");
    this.modeBtn.classList.add("tile__mode");
    this.modeBtn.addEventListener("click", () => this.setMode(this.mode === "watch" ? "live" : "watch"));
    const maxBtn = headButton(ICON_EXPAND, "Fill the screen with this tile (again to restore)");
    maxBtn.addEventListener("click", () => hooks.onMaximize(this));
    const mainBtn = headButton(ICON_OPEN, "Open as the main session");
    mainBtn.addEventListener("click", () => hooks.onOpenMain(this.session));
    const closeBtn = headButton(ICON_X, "Remove this tile (the session keeps running)");
    closeBtn.addEventListener("click", () => hooks.onClose(this));
    this.head.append(this.dot, this.nameEl, this.modeBtn, maxBtn, mainBtn, closeBtn);
    this.body = document.createElement("div");
    this.body.className = "term-pane";
    this.el.append(this.head, this.body);
    // Capture phase: runs before xterm's own handlers swallow the event.
    this.el.addEventListener("pointerdown", () => hooks.onFocus(this), true);
    this.render();
    this.start();
  }

  /** The socket typed input should go to, or null if this tile can't take
   *  input right now (watching, or the session ended). */
  get inputSocket(): TerminalSocket | null {
    return this.mode === "live" && !this.gone ? this.socket : null;
  }

  setFocused(f: boolean): void {
    this.focused = f;
    this.el.classList.toggle("tile--focused", f);
    if (f) this.el.classList.remove("tile--activity");
  }

  focusTerminal(): void {
    try { this.api?.term.focus(); } catch {}
  }

  setMode(mode: TileMode): void {
    if (mode === this.mode || this.disposed) return;
    this.mode = mode;
    this.stop();
    this.render();
    this.start();
    this.hooks.onModeChange(this, mode);
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.stop();
    if (this.activityTimer !== null) clearTimeout(this.activityTimer);
    this.el.remove();
  }

  private render(): void {
    this.nameEl.textContent = this.session;
    this.nameEl.title = this.session;
    const watch = this.mode === "watch";
    this.modeBtn.textContent = watch ? "watching" : "live";
    this.modeBtn.title = watch
      ? "Watching: read-only, never resizes the session. Click (or type) to interact."
      : "Live: typing goes to this session, and it's sized to this tile. Click to just watch.";
    this.el.classList.toggle("tile--watch", watch);
  }

  private start(): void {
    this.gone = false;
    this.el.classList.remove("tile--gone");
    this.body.replaceChildren();
    const watch = this.mode === "watch";
    const socket = new TerminalSocket(this.urlFor(this.session, this.mode), {
      onStatus: (status) => {
        this.dot.dataset.state = status === "open" ? "ok" : status === "closed" ? "err" : "warn";
        if (status === "open" && this.pendingInput.length && this.mode === "live") {
          for (const data of this.pendingInput) socket.send({ type: "input", data });
          this.pendingInput = [];
        }
      },
      onHello: (msg) => {
        if (!socket.seamlessReconnect) {
          this.api?.resetBuffer();
          this.bottomOnNextOutput = true;
        }
        if (watch && msg.cols && msg.rows) this.api?.setFixedSize(msg.cols, msg.rows);
      },
      // The watched session changed size. Re-wrapping the buffer to the new
      // width and then taking tmux's redraw on top duplicates rows, so start
      // over: a fresh attach replays the history at the new size.
      onSize: () => {
        if (!watch) return;
        window.setTimeout(() => {
          if (this.disposed || this.socket !== socket) return;
          this.stop();
          this.start();
        }, 0);
      },
      onOutput: (data) => {
        if (this.bottomOnNextOutput) {
          this.bottomOnNextOutput = false;
          this.api?.writeToTerm(data, () => this.api?.scrollToBottom());
        } else {
          this.api?.writeToTerm(data);
        }
        this.noteActivity();
      },
      onSessionGone: () => this.markGone(),
      onAuthRevoked: () => { /* the main terminal re-bootstraps the app */ },
    });
    this.socket = socket;
    // Just switched to live because the user started typing: keystrokes made
    // before the new connection opens would be dropped, so queue them too.
    const queueing = this.pendingInput.length > 0;
    const termSocket = !queueing ? socket : new Proxy(socket, {
      get: (target, prop) => prop === "send"
        ? (msg: Parameters<TerminalSocket["send"]>[0]) => {
            if (msg.type === "input" && !target.isOpen) this.pendingInput.push(msg.data);
            else target.send(msg);
          }
        : Reflect.get(target, prop, target),
    });
    const api = this.createTerminal(this.body, termSocket, loadDisplayPrefs(this.session), this.session, {
      autoFocus: false,
      isActive: () => this.focused,
      watch,
      webgl: false,
    });
    this.api = api;
    if (watch) {
      // Typing into a watch tile means "let me interact": go live and send
      // what was typed once the live connection is open.
      // onKey, not onData: onData also carries xterm's automatic replies to
      // tmux's terminal queries, which aren't the user typing.
      api.term.onKey(({ key }) => {
        if (this.mode !== "watch" || this.gone) return;
        this.pendingInput.push(key);
        // Deferred: this handler runs inside the terminal it would dispose.
        window.setTimeout(() => this.setMode("live"), 0);
      });
    }
    api.term.textarea?.addEventListener("focus", () => this.hooks.onFocus(this));
    this.unsubPrefs = onDisplayPrefsChange((p) => api.applyPrefs(p));
    socket.connect();
    if (this.focused) this.focusTerminal();
  }

  private stop(): void {
    try { this.socket?.close(); } catch {}
    try { this.api?.dispose(); } catch {}
    this.unsubPrefs?.();
    this.socket = null;
    this.api = null;
    this.unsubPrefs = null;
  }

  private markGone(): void {
    this.gone = true;
    try { this.socket?.close(); } catch {}
    this.el.classList.add("tile--gone");
    this.dot.dataset.state = "err";
    const note = document.createElement("div");
    note.className = "tile__gone";
    note.textContent = `${this.session} has ended`;
    this.body.append(note);
  }

  /** Output on a tile you aren't looking at lights its header briefly. */
  private noteActivity(): void {
    if (this.focused) return;
    this.el.classList.add("tile--activity");
    if (this.activityTimer !== null) clearTimeout(this.activityTimer);
    this.activityTimer = window.setTimeout(() => {
      this.activityTimer = null;
      this.el.classList.remove("tile--activity");
    }, 1500);
  }
}

export function headButton(label: string, title: string): HTMLButtonElement {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "tile__btn";
  // Icons are our own constant SVG strings; text labels go in as text.
  if (label.startsWith("<svg")) b.innerHTML = label;
  else b.textContent = label;
  b.title = title;
  b.setAttribute("aria-label", title || label);
  return b;
}
