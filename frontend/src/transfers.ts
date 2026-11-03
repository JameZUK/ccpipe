// Uploads and downloads that survive a bad connection.
//
// Uploads go in pieces (the server's resumable upload API, backend
// routes/fs_transfer.py). A piece that fails is retried after a growing
// pause, from however much the server says has arrived, so a dropped
// signal costs at most the piece in flight, not the whole file. Pieces
// start at 1 MiB, double while the link is quick and halve when one fails.
//
// Downloads are fetched in byte ranges the same way and put together in
// the browser, then saved. Files too big to hold in memory go to the
// browser's own download instead, which can resume them since the server
// honours ranges.
//
// Either way a transfer that stalls (no bytes for STALL_MS) is cut and
// retried, and coming back online retries at once. Transfers belong to the
// page, not the file panel: closing the panel doesn't stop them, and the
// panel lists them again when reopened. While any is running, the page
// won't reload itself for an update and asks before being closed.

import { addBusyCheck } from "./update";

export type TransferState = "queued" | "running" | "retrying" | "done" | "failed" | "cancelled";

export interface Transfer {
  readonly id: number;
  readonly kind: "upload" | "download";
  readonly name: string;
  /** Upload: where it goes. Download: the file. */
  readonly path: string;
  size: number;
  done: number;
  state: TransferState;
  /** Why it's retrying or failed. */
  note: string;
  /** Bytes per second, smoothed. */
  rate: number;
  cancel(): void;
  /** Start again after "failed", from where it got to. */
  retry(): void;
}

const MiB = 1024 * 1024;
const PIECE_MIN = 256 * 1024;
const PIECE_START = MiB;
const PIECE_MAX = 8 * MiB;
/** No bytes moving for this long: cut the piece and retry. */
const STALL_MS = 25_000;
/** Pauses between retries (ms); the last repeats. */
const BACKOFF = [1000, 2000, 4000, 8000, 15000, 30000];
/** Consecutive failures before giving up (about 10 minutes of trying). */
const MAX_FAILURES = 25;
/** Bigger downloads use the browser's own (resumable) download. */
const MEMORY_DOWNLOAD_MAX = 300 * MiB;
const HEADERS = { "X-Requested-By": "ccpipe" };

const all: Transfer[] = [];
const listeners = new Set<(t: readonly Transfer[]) => void>();
let notifyQueued = false;
let nextId = 1;
let wakeRetries: (() => void)[] = [];
let installed = false;

function notify(): void {
  if (notifyQueued) return;
  notifyQueued = true;
  requestAnimationFrame(() => {
    notifyQueued = false;
    for (const cb of listeners) cb(all);
  });
}

/** Called with the list whenever anything changes. Returns unsubscribe. */
export function onTransfers(cb: (t: readonly Transfer[]) => void): () => void {
  listeners.add(cb);
  cb(all);
  return () => { listeners.delete(cb); };
}

function active(t: Transfer): boolean {
  return t.state === "queued" || t.state === "running" || t.state === "retrying";
}

export function activeCount(): number {
  return all.filter(active).length;
}

export function clearFinished(): void {
  for (let i = all.length - 1; i >= 0; i--) {
    if (all[i].state === "done" || all[i].state === "cancelled") all.splice(i, 1);
  }
  notify();
}

function install(): void {
  if (installed) return;
  installed = true;
  addBusyCheck(() => activeCount() > 0);
  window.addEventListener("beforeunload", (e) => {
    if (activeCount() > 0) { e.preventDefault(); e.returnValue = ""; }
  });
  window.addEventListener("online", () => {
    const w = wakeRetries;
    wakeRetries = [];
    for (const f of w) f();
  });
}

class Cancelled extends Error {}
class Fatal extends Error {}

/** A pause that ends early when the connection comes back, or rejects
 * on cancel. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new Cancelled()); return; }
    const done = (): void => { clearTimeout(timer); signal.removeEventListener("abort", onAbort); resolve(); };
    const onAbort = (): void => { clearTimeout(timer); reject(new Cancelled()); };
    const timer = window.setTimeout(done, ms);
    signal.addEventListener("abort", onAbort, { once: true });
    wakeRetries.push(done);
  });
}

function errorText(e: unknown): string {
  if (e instanceof Error) return e.message || e.name;
  return String(e);
}

interface Runner {
  t: Transfer;
  ctl: AbortController;
  failures: number;
  /** For the rate estimate. */
  lastAt: number;
  lastDone: number;
}

/** A transfer, listed at once. *run* starts when *gate* resolves (the one
 * before it in a queue has finished). */
function makeTransfer(kind: Transfer["kind"], name: string, path: string, size: number,
                      run: (r: Runner) => Promise<void>, gate: Promise<void> = Promise.resolve()): Transfer {
  install();
  let r: Runner = { t: null as unknown as Transfer, ctl: new AbortController(), failures: 0, lastAt: 0, lastDone: 0 };
  const t: Transfer = {
    id: nextId++, kind, name, path, size, done: 0, state: "queued", note: "", rate: 0,
    cancel: () => {
      if (t.state === "done" || t.state === "cancelled") return;
      r.ctl.abort();
      t.state = "cancelled";
      t.note = "";
      notify();
    },
    retry: () => {
      if (t.state === "failed") start();
    },
  };
  r.t = t;
  const start = (): void => {
    r = { t, ctl: new AbortController(), failures: 0, lastAt: performance.now(), lastDone: t.done };
    t.state = "running";
    t.note = "";
    notify();
    run(r).then(() => {
      t.state = "done";
      t.done = t.size;
      t.note = "";
      notify();
    }, (e: unknown) => {
      if (e instanceof Cancelled || t.state === "cancelled") return;
      t.state = "failed";
      t.note = errorText(e);
      notify();
    });
  };
  all.push(t);
  notify();
  void gate.then(() => { if (t.state === "queued") start(); });
  return t;
}

/** Resolves once *t* has stopped running, whatever the outcome. */
function settled(t: Transfer): Promise<void> {
  return new Promise((resolve) => {
    const unsub = onTransfers(() => {
      if (active(t)) return;
      queueMicrotask(() => unsub());
      resolve();
    });
  });
}

function progress(r: Runner, done: number): void {
  const t = r.t;
  t.done = done;
  const now = performance.now();
  const dt = now - r.lastAt;
  if (dt >= 500) {
    const inst = ((done - r.lastDone) * 1000) / dt;
    t.rate = t.rate ? t.rate * 0.6 + inst * 0.4 : inst;
    r.lastAt = now;
    r.lastDone = done;
  }
  if (t.state === "retrying") { t.state = "running"; t.note = ""; }
  notify();
}

/** After a failed attempt: count it, give up if there have been too many
 * in a row, otherwise show it and wait before the next. */
async function failed(r: Runner, why: string): Promise<void> {
  r.failures++;
  if (r.failures > MAX_FAILURES) throw new Fatal(`gave up: ${why}`);
  const ms = BACKOFF[Math.min(r.failures - 1, BACKOFF.length - 1)];
  r.t.state = "retrying";
  r.t.note = `${why}; retrying in ${Math.round(ms / 1000)} s`;
  r.t.rate = 0;
  notify();
  await pause(ms, r.ctl.signal);
}

// ── uploads ─────────────────────────────────────────────────────────────

interface Sent { status: number; body: { received?: number; detail?: string } | null }

/** PUT one piece with XHR (fetch can't report upload progress), cut off
 * if nothing moves for STALL_MS. Resolves with the server's answer;
 * rejects only on a network failure. */
function putPiece(url: string, blob: Blob, signal: AbortSignal, onSent: (n: number) => void): Promise<Sent> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.withCredentials = true;
    xhr.setRequestHeader("X-Requested-By", "ccpipe");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    let stall = 0;
    const arm = (): void => {
      clearTimeout(stall);
      stall = window.setTimeout(() => xhr.abort(), STALL_MS);
    };
    const onAbort = (): void => xhr.abort();
    signal.addEventListener("abort", onAbort, { once: true });
    const finish = (): void => { clearTimeout(stall); signal.removeEventListener("abort", onAbort); };
    xhr.upload.onprogress = (e) => { arm(); onSent(e.loaded); };
    xhr.onload = () => {
      finish();
      let body: Sent["body"] = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => { finish(); reject(new Error("connection lost")); };
    xhr.onabort = () => {
      finish();
      reject(signal.aborted ? new Cancelled() : new Error("connection stalled"));
    };
    arm();
    xhr.send(blob);
  });
}

/** fetch() with the CSRF header; a network failure becomes an Error
 * (a cancel, Cancelled). */
async function call(url: string, init: RequestInit, signal: AbortSignal): Promise<Response> {
  try {
    return await fetch(url, {
      credentials: "same-origin", ...init,
      headers: { ...HEADERS, ...(init.headers as Record<string, string> | undefined) }, signal,
    });
  } catch (e) {
    if (signal.aborted) throw new Cancelled();
    throw new Error("connection lost", { cause: e });
  }
}

async function detail(res: Response): Promise<string> {
  try {
    const d = await res.json();
    return String(d.detail ?? `HTTP ${res.status}`);
  } catch {
    return `HTTP ${res.status}`;
  }
}

/** A 4xx other than a conflict or a rate limit is the request's fault,
 * not the link's: retrying won't help. */
function fatalStatus(status: number): boolean {
  return status >= 400 && status < 500 && status !== 408 && status !== 409 && status !== 429;
}

/** What survives a "retry" after a failure: the server-side upload. */
interface UploadState { id: string | null }

async function runUpload(r: Runner, file: File, target: string, st: UploadState): Promise<void> {
  const sig = r.ctl.signal;
  let id: string | null = st.id;
  let offset = 0;
  if (id !== null) {
    // A retry after "failed": carry on from what the server has.
    try {
      const res = await call(`/api/fs/uploads/${id}`, {}, sig);
      if (res.ok) offset = (await res.json()).received;
      else id = st.id = null;
    } catch (e) {
      if (e instanceof Cancelled) throw e;
    }
  }
  let piece = PIECE_START;
  let pieceMax = PIECE_MAX;

  for (;;) {
    try {
      if (id === null) {
        const res = await call("/api/fs/uploads", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: target, size: file.size }),
        }, sig);
        if (!res.ok) {
          if (fatalStatus(res.status)) throw new Fatal(await detail(res));
          throw new Error(await detail(res));
        }
        const d = await res.json();
        id = st.id = d.id as string;
        pieceMax = Math.min(PIECE_MAX, d.chunk_max ?? PIECE_MAX);
        offset = 0;
        progress(r, 0);
      }
      while (offset < file.size) {
        const end = Math.min(file.size, offset + piece);
        const t0 = performance.now();
        const from = offset;
        const sent = await putPiece(`/api/fs/uploads/${id}?offset=${from}`, file.slice(from, end), sig,
          (n) => progress(r, from + n));
        if (sent.status === 404 || sent.status === 410) { id = st.id = null; throw new Error("upload expired; starting again"); }
        if (sent.status === 409 && typeof sent.body?.received === "number") {
          offset = sent.body.received;
          continue;
        }
        if (sent.status < 200 || sent.status >= 300) {
          const why = sent.body?.detail ?? `HTTP ${sent.status}`;
          if (fatalStatus(sent.status)) throw new Fatal(why);
          throw new Error(why);
        }
        offset = sent.body?.received ?? end;
        progress(r, offset);
        r.failures = 0;
        if (performance.now() - t0 < 2000) piece = Math.min(pieceMax, piece * 2);
      }
      const res = await call(`/api/fs/uploads/${id}/finish`, { method: "POST" }, sig);
      if (res.ok) return;
      if (res.status === 409) {
        const d = await res.json().catch(() => ({}));
        if (typeof d.received === "number") { offset = d.received; continue; }
      }
      if (res.status === 404) {
        // The finish may have worked, with its answer lost on the way back.
        const stat = await call(`/api/fs/stat?path=${encodeURIComponent(target)}`, {}, sig);
        if (stat.ok && (await stat.json()).size === file.size) return;
        id = st.id = null;
        throw new Error("upload expired; starting again");
      }
      if (fatalStatus(res.status)) throw new Fatal(await detail(res));
      throw new Error(await detail(res));
    } catch (e) {
      if (e instanceof Cancelled || e instanceof Fatal) {
        if (e instanceof Cancelled && id) {
          void call(`/api/fs/uploads/${id}`, { method: "DELETE" }, new AbortController().signal).catch(() => {});
        }
        throw e;
      }
      piece = Math.max(PIECE_MIN, Math.floor(piece / 2));
      await failed(r, errorText(e));
      // Where did we get to? (The failed piece may have half-arrived.)
      if (id !== null) {
        try {
          const res = await call(`/api/fs/uploads/${id}`, {}, sig);
          if (res.ok) offset = (await res.json()).received;
          else if (res.status === 404 || res.status === 410) id = st.id = null;
        } catch (e2) {
          if (e2 instanceof Cancelled) throw e2;
          // Still offline: the next attempt finds out.
        }
      }
    }
  }
}

/** Upload *files* into directory *dir*, one after another. *onDone* runs
 * after each file lands (to refresh a listing). */
export function uploadFiles(dir: string, files: File[], onDone?: (path: string) => void): void {
  const base = dir.endsWith("/") ? dir : `${dir}/`;
  let gate: Promise<void> = Promise.resolve();
  for (const f of files) {
    const target = base + f.name;
    const st: UploadState = { id: null };
    const t = makeTransfer("upload", f.name, target, f.size, (r) => runUpload(r, f, target, st), gate);
    gate = settled(t).then(() => { if (t.state === "done") onDone?.(target); });
  }
}

// ── downloads ───────────────────────────────────────────────────────────

/** Read *res*'s body into *parts*, reporting each piece; cut off if it
 * stalls. */
async function readBody(res: Response, parts: Uint8Array[], sig: AbortSignal,
                        onBytes: (n: number) => void): Promise<void> {
  const reader = res.body!.getReader();
  let stall = 0;
  const arm = (): void => {
    clearTimeout(stall);
    stall = window.setTimeout(() => { void reader.cancel(new Error("connection stalled")); }, STALL_MS);
  };
  arm();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value);
      onBytes(value.byteLength);
      arm();
    }
  } catch (e) {
    if (sig.aborted) throw new Cancelled();
    throw new Error("connection lost", { cause: e });
  } finally {
    clearTimeout(stall);
  }
}

function saveBlob(blob: Blob, name: string): void {
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  a.style.display = "none";
  document.body.append(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 60_000);
}

/** What survives a "retry" after a failure: what has arrived. */
interface DownloadState { parts: Uint8Array[]; have: number; tag: string | null }

async function runDownload(r: Runner, path: string, name: string, st: DownloadState): Promise<void> {
  const sig = r.ctl.signal;
  const url = `/api/fs/download?path=${encodeURIComponent(path)}`;
  const parts = st.parts;
  let piece = 2 * MiB;

  for (;;) {
    if (st.tag !== null && st.have >= r.t.size) break;
    try {
      const end = (st.tag === null ? piece : Math.min(r.t.size, st.have + piece)) - 1;
      const headers: Record<string, string> = { Range: `bytes=${st.have}-${end}` };
      if (st.tag) headers["If-Range"] = st.tag;
      const t0 = performance.now();
      const res = await call(url, { headers }, sig);
      if (res.status === 416 && st.have === 0) {        // an empty file
        r.t.size = 0;
        break;
      }
      if (!res.ok) {
        if (fatalStatus(res.status)) throw new Fatal(await detail(res));
        throw new Error(await detail(res));
      }
      if (res.status === 200) {
        // The whole file: it changed since the first piece (If-Range), so
        // what we have is stale. Start over with this response.
        parts.length = 0;
        st.have = 0;
        r.t.size = Number(res.headers.get("Content-Length") ?? r.t.size);
      } else {
        const total = (res.headers.get("Content-Range") ?? "").match(/\/(\d+)$/);
        if (total) r.t.size = Number(total[1]);
      }
      st.tag = res.headers.get("ETag") ?? "";
      await readBody(res, parts, sig, (n) => { st.have += n; progress(r, st.have); });
      r.failures = 0;
      if (res.status === 200) break;
      if (performance.now() - t0 < 2000) piece = Math.min(PIECE_MAX, piece * 2);
    } catch (e) {
      if (e instanceof Cancelled || e instanceof Fatal) throw e;
      piece = Math.max(PIECE_MIN, Math.floor(piece / 2));
      // Keep what arrived: the next range starts after it.
      await failed(r, errorText(e));
    }
  }
  saveBlob(new Blob(parts as BlobPart[], { type: "application/octet-stream" }), name);
}

/** Download the file at *path*. *size* (from the listing) picks how: in
 * pieces with retries, or for very big files the browser's own download. */
export function downloadFile(path: string, name: string, size: number): void {
  if (size > MEMORY_DOWNLOAD_MAX) {
    const a = document.createElement("a");
    a.href = `/api/fs/download?path=${encodeURIComponent(path)}`;
    a.download = name;
    a.style.display = "none";
    document.body.append(a);
    a.click();
    a.remove();
    return;
  }
  const st: DownloadState = { parts: [], have: 0, tag: null };
  makeTransfer("download", name, path, size, (r) => runDownload(r, path, name, st));
}
