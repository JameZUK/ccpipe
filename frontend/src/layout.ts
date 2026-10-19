// Tiled screen layouts (iPad / desktop): other sessions shown beside the
// main terminal, arranged as a split tree.
//
// A layout belongs to the main session it was made in and is stored on the
// server (/api/layouts/<session>), so opening that session on any device
// brings its tiles back. The main terminal (#terminal, owned by main.ts) is
// one leaf of the tree; every other leaf is a Tile (tile.ts).
//
// Tiles need room: below TILE_MIN_WIDTH (phones) the layout is kept but
// not shown, and the main terminal fills the screen as usual.

import { getLayout, putLayout, type LayoutNode } from "./api";
import type { createTerminal as CreateTerminal } from "./terminal";
import { ICON_EXPAND } from "./icons";
import { Tile, headButton, type TileMode } from "./tile";
import type { TerminalSocket } from "./ws";

const TILE_MIN_WIDTH = 800;
const MAX_TILES = 9;          // backend: MAX_LEAVES
const MAX_CHILDREN = 4;       // backend: MAX_CHILDREN
const MIN_SHARE = 0.08;       // smallest a divider drag can squeeze a tile

type Leaf = Extract<LayoutNode, { t: "leaf" }>;
type Split = Extract<LayoutNode, { t: "split" }>;
export type Preset = "side" | "grid" | "main-stack" | "stack";

export interface TileLayoutDeps {
  owner: string;
  /** The main terminal's container (#terminal). */
  mainEl: HTMLElement;
  urlFor(session: string, mode: TileMode): string;
  createTerminal: typeof CreateTerminal;
  /** Focus moved: session null = the main terminal. */
  onFocusChange(session: string | null, socket: TerminalSocket | null): void;
  openMain(session: string): void;
}

export class TileLayout {
  private tree: LayoutNode | null = null;
  private readonly tiles = new Map<string, Tile>();
  private focused: string | null = null;     // null = the main terminal
  private maximized: string | null = null;   // session, or owner for main
  private readonly mainWrap: HTMLDivElement;
  private saveTimer: number | null = null;
  private disposed = false;
  private wasCapable: boolean;

  constructor(private readonly root: HTMLElement, private readonly deps: TileLayoutDeps) {
    // The main terminal's tile frame, used only while tiles are shown.
    this.mainWrap = document.createElement("div");
    this.mainWrap.className = "tile tile--main";
    const head = document.createElement("div");
    head.className = "tile__head";
    const dot = document.createElement("span");
    dot.className = "tile__dot";
    dot.dataset.state = "main";
    const name = document.createElement("span");
    name.className = "tile__name";
    name.textContent = deps.owner;
    const badge = document.createElement("span");
    badge.className = "tile__badge";
    badge.textContent = "main";
    const maxBtn = headButton(ICON_EXPAND, "Fill the screen with this tile (again to restore)");
    maxBtn.addEventListener("click", () => this.toggleMaximize(deps.owner));
    head.append(dot, name, badge, maxBtn);
    this.mainWrap.append(head);
    this.mainWrap.addEventListener("pointerdown", () => this.setFocus(null), true);
    deps.mainEl.addEventListener("focusin", this.onMainFocusIn);
    this.wasCapable = this.capable();
    window.addEventListener("resize", this.onWindowResize);
  }

  /** Fetch the owner's saved layout and show it. */
  async load(): Promise<void> {
    try {
      const tree = await getLayout(this.deps.owner);
      if (this.disposed) return;
      this.tree = tree && this.sanitize(tree);
    } catch (e) {
      console.warn("layout load failed:", e);
    }
    this.render();
  }

  capable(): boolean {
    return window.innerWidth >= TILE_MIN_WIDTH;
  }

  /** Sessions currently tiled (excluding the main one), in tree order. */
  tiledSessions(): string[] {
    return this.leaves().map((l) => l.s).filter((s) => s !== this.deps.owner);
  }

  isOwner(session: string): boolean {
    return session === this.deps.owner;
  }

  get tileCount(): number {
    return this.leaves().length;
  }

  /** Add *session* as a new tile beside ("h") or below ("v") the focused one. */
  add(session: string, dir: "h" | "v"): void {
    if (session === this.deps.owner || this.leaves().some((l) => l.s === session)) return;
    if (this.tileCount >= MAX_TILES) return;
    const fresh: Leaf = { t: "leaf", s: session, m: "watch" };
    const target = this.focused ?? this.deps.owner;
    const tree = this.tree ?? { t: "leaf", s: this.deps.owner, m: "live" } as Leaf;
    this.tree = insertBeside(tree, target, fresh, dir);
    this.maximized = null;
    this.commit();
    this.setFocus(session);
  }

  remove(session: string): void {
    if (!this.tree || session === this.deps.owner) return;
    this.tree = removeLeaf(this.tree, session);
    if (this.tree && this.tree.t === "leaf") this.tree = null;
    if (this.focused === session) this.setFocus(null);
    if (this.maximized === session) this.maximized = null;
    this.commit();
  }

  clear(): void {
    this.tree = null;
    this.maximized = null;
    this.setFocus(null);
    this.commit();
  }

  preset(kind: Preset): void {
    const leaves = this.leaves();
    const main = leaves.find((l) => l.s === this.deps.owner)
      ?? { t: "leaf", s: this.deps.owner, m: "live" } as Leaf;
    const others = leaves.filter((l) => l.s !== this.deps.owner);
    if (!others.length) return;
    this.tree = buildPreset(kind, main, others);
    this.maximized = null;
    this.commit();
  }

  toggleMaximize(session: string): void {
    this.maximized = this.maximized === session ? null : session;
    this.render();
  }

  dispose(): void {
    this.disposed = true;
    window.removeEventListener("resize", this.onWindowResize);
    this.deps.mainEl.removeEventListener("focusin", this.onMainFocusIn);
    if (this.saveTimer !== null) { clearTimeout(this.saveTimer); this.flushSave(); }
    for (const t of this.tiles.values()) t.dispose();
    this.tiles.clear();
  }

  // ── internals ────────────────────────────────────────────────────────

  private onWindowResize = (): void => {
    const c = this.capable();
    if (c !== this.wasCapable) { this.wasCapable = c; this.render(); }
  };

  private onMainFocusIn = (): void => { this.setFocus(null); };

  private leaves(): Leaf[] {
    const out: Leaf[] = [];
    const walk = (n: LayoutNode): void => {
      if (n.t === "leaf") out.push(n); else n.c.forEach(walk);
    };
    if (this.tree) walk(this.tree);
    return out;
  }

  /** Drop duplicate sessions; make sure the main session is in the tree. */
  private sanitize(tree: LayoutNode): LayoutNode | null {
    const seen = new Set<string>();
    const dedupe = (n: LayoutNode): LayoutNode | null => {
      if (n.t === "leaf") {
        if (seen.has(n.s)) return null;
        seen.add(n.s);
        return { ...n };
      }
      const kids: LayoutNode[] = [];
      const sz: number[] = [];
      n.c.forEach((k, i) => {
        const d = dedupe(k);
        if (d) { kids.push(d); sz.push(n.sz[i] ?? 1); }
      });
      if (!kids.length) return null;
      if (kids.length === 1) return kids[0];
      return { t: "split", d: n.d, sz, c: kids };
    };
    let out = dedupe(tree);
    if (out && !seen.has(this.deps.owner)) {
      out = { t: "split", d: "h", sz: [1, 1], c: [{ t: "leaf", s: this.deps.owner, m: "live" }, out] };
    }
    return out && out.t === "leaf" ? null : out;
  }

  private setFocus(session: string | null): void {
    if (session !== null && !this.tiles.has(session)) session = null;
    const changed = session !== this.focused;
    this.focused = session;
    for (const [s, t] of this.tiles) t.setFocused(s === session);
    this.mainWrap.classList.toggle("tile--focused", session === null && this.tiles.size > 0);
    if (changed) this.announceFocus();
  }

  private announceFocus(): void {
    const tile = this.focused ? this.tiles.get(this.focused) : undefined;
    this.deps.onFocusChange(tile ? tile.session : null, tile?.inputSocket ?? null);
  }

  /** Save (debounced) and re-render after a change to the tree. */
  private commit(): void {
    this.render();
    if (this.saveTimer !== null) clearTimeout(this.saveTimer);
    this.saveTimer = window.setTimeout(() => this.flushSave(), 400);
  }

  private flushSave(): void {
    this.saveTimer = null;
    void putLayout(this.deps.owner, this.tree).catch((e) => console.warn("layout save failed:", e));
  }

  private render(): void {
    if (this.disposed) return;
    // Re-parenting a focused terminal (below) drops its keyboard focus;
    // note it now and put it back afterwards.
    const hadFocus = document.activeElement as HTMLElement | null;
    const show = this.tree !== null && this.capable();
    const wanted = new Set(show ? this.tiledSessions() : []);
    // Tiles no longer in the tree (or not shown at all) go away.
    for (const [s, t] of this.tiles) {
      if (!wanted.has(s)) { t.dispose(); this.tiles.delete(s); }
    }
    if (!show) {
      this.root.classList.remove("tiles--on");
      this.root.replaceChildren(this.deps.mainEl);
      this.setFocus(null);
      return;
    }
    for (const leaf of this.leaves()) {
      if (leaf.s === this.deps.owner || this.tiles.has(leaf.s)) continue;
      this.tiles.set(leaf.s, new Tile(leaf.s, leaf.m, this.deps.urlFor, this.deps.createTerminal, {
        onFocus: (t) => this.setFocus(t.session),
        onClose: (t) => this.remove(t.session),
        onModeChange: (t, mode) => {
          const l = this.leaves().find((x) => x.s === t.session);
          if (l) l.m = mode;
          this.commit();
          if (this.focused === t.session) this.announceFocus();
        },
        onMaximize: (t) => this.toggleMaximize(t.session),
        onOpenMain: (s) => this.deps.openMain(s),
      }));
    }
    if (this.mainWrap.lastElementChild !== this.deps.mainEl) this.mainWrap.append(this.deps.mainEl);
    this.root.classList.add("tiles--on");
    const max = this.maximized;
    const dom = max ? this.leafEl(max) : this.build(this.tree!);
    this.root.replaceChildren(dom ?? this.build(this.tree!));
    if (hadFocus && hadFocus !== document.body && hadFocus.isConnected
        && document.activeElement !== hadFocus) {
      try { hadFocus.focus({ preventScroll: true }); } catch {}
    }
    this.setFocus(this.focused);
    this.mainWrap.classList.toggle("tile--focused", this.focused === null);
  }

  private leafEl(session: string): HTMLElement | null {
    if (session === this.deps.owner) return this.mainWrap;
    return this.tiles.get(session)?.el ?? null;
  }

  private build(node: LayoutNode): HTMLElement {
    if (node.t === "leaf") {
      return this.leafEl(node.s) ?? document.createElement("div");
    }
    const box = document.createElement("div");
    box.className = `tsplit tsplit--${node.d}`;
    const cells: HTMLElement[] = [];
    node.c.forEach((kid, i) => {
      if (i > 0) box.append(this.divider(node, i - 1, box, cells));
      const cell = document.createElement("div");
      cell.className = "tsplit__cell";
      cell.style.flex = `${node.sz[i]} 1 0`;
      cell.append(this.build(kid));
      cells.push(cell);
      box.append(cell);
    });
    return box;
  }

  /** A draggable bar between cells i and i+1 of *split*. */
  private divider(split: Split, i: number, box: HTMLElement, cells: HTMLElement[]): HTMLElement {
    const bar = document.createElement("div");
    bar.className = "tsplit__divider";
    bar.setAttribute("role", "separator");
    bar.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      bar.setPointerCapture(e.pointerId);
      bar.classList.add("dragging");
      const horiz = split.d === "h";
      const rect = box.getBoundingClientRect();
      const total = horiz ? rect.width : rect.height;
      const start = horiz ? e.clientX : e.clientY;
      const pair = split.sz[i] + split.sz[i + 1];
      const a0 = split.sz[i];
      const move = (ev: PointerEvent): void => {
        const delta = ((horiz ? ev.clientX : ev.clientY) - start) / Math.max(1, total);
        const a = Math.min(pair - MIN_SHARE, Math.max(MIN_SHARE, a0 + delta));
        split.sz[i] = a;
        split.sz[i + 1] = pair - a;
        cells[i].style.flex = `${split.sz[i]} 1 0`;
        cells[i + 1].style.flex = `${split.sz[i + 1]} 1 0`;
      };
      const up = (): void => {
        bar.classList.remove("dragging");
        bar.removeEventListener("pointermove", move);
        bar.removeEventListener("pointerup", up);
        bar.removeEventListener("pointercancel", up);
        if (this.saveTimer !== null) clearTimeout(this.saveTimer);
        this.saveTimer = window.setTimeout(() => this.flushSave(), 400);
      };
      bar.addEventListener("pointermove", move);
      bar.addEventListener("pointerup", up);
      bar.addEventListener("pointercancel", up);
    });
    return bar;
  }
}

// ── tree operations (pure) ──────────────────────────────────────────────

function insertBeside(node: LayoutNode, target: string, fresh: Leaf, dir: "h" | "v"): LayoutNode {
  if (node.t === "leaf") {
    if (node.s !== target) return node;
    return { t: "split", d: dir, sz: [1, 1], c: [node, fresh] };
  }
  const i = node.c.findIndex((k) => k.t === "leaf" && k.s === target);
  if (i >= 0 && node.d === dir && node.c.length < MAX_CHILDREN) {
    // Same direction: join this split rather than nesting another. The new
    // tile takes half of the target's share.
    const c = [...node.c];
    const sz = [...node.sz];
    const half = sz[i] / 2;
    sz[i] = half;
    c.splice(i + 1, 0, fresh);
    sz.splice(i + 1, 0, half);
    return { ...node, c, sz };
  }
  return { ...node, c: node.c.map((k) => insertBeside(k, target, fresh, dir)) };
}

function removeLeaf(node: LayoutNode, session: string): LayoutNode | null {
  if (node.t === "leaf") return node.s === session ? null : node;
  const c: LayoutNode[] = [];
  const sz: number[] = [];
  node.c.forEach((k, i) => {
    const r = removeLeaf(k, session);
    if (r) { c.push(r); sz.push(node.sz[i]); }
  });
  if (!c.length) return null;
  if (c.length === 1) return c[0];
  return { ...node, c, sz };
}

/** Split *items* into rows/columns of at most MAX_CHILDREN (a grid if more). */
function line(dir: "h" | "v", items: LayoutNode[]): LayoutNode {
  if (items.length === 1) return items[0];
  if (items.length <= MAX_CHILDREN) return { t: "split", d: dir, sz: items.map(() => 1), c: items };
  return grid(items);
}

function grid(items: LayoutNode[]): LayoutNode {
  if (items.length === 1) return items[0];
  const cols = Math.min(MAX_CHILDREN, Math.ceil(Math.sqrt(items.length)));
  const rows: LayoutNode[] = [];
  for (let i = 0; i < items.length; i += cols) {
    const row = items.slice(i, i + cols);
    rows.push(row.length === 1 ? row[0] : { t: "split", d: "h", sz: row.map(() => 1), c: row });
  }
  return rows.length === 1 ? rows[0] : { t: "split", d: "v", sz: rows.map(() => 1), c: rows };
}

function buildPreset(kind: Preset, main: Leaf, others: Leaf[]): LayoutNode {
  const all = [main, ...others];
  switch (kind) {
    case "side": return line("h", all);
    case "stack": return line("v", all);
    case "grid": return grid(all);
    case "main-stack":
      return { t: "split", d: "h", sz: [0.6, 0.4], c: [main, line("v", others)] };
  }
}
