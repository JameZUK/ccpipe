// Shared API helper used by every module that talks to the ccpipe
// backend. Centralises:
//
//   - same-origin credentials so the session cookie always rides along
//   - the X-Requested-By CSRF header (the backend's CsrfDep checks it)
//   - default Content-Type for JSON bodies
//   - error normalisation: pulls { detail } out of the failure response
//     so callers see Error("detail string") rather than having to dig
//     through the Response themselves
//
// Two entry points:
//   apiJson<T>()   — for endpoints that return JSON (the common case)
//   apiVoid()      — for endpoints that return nothing (logout, etc.)
//   apiRaw()       — for callers that need the raw Response (download,
//                    streaming, header inspection)

const CSRF_HEADERS = {
  "X-Requested-By": "ccpipe",
} as const;

function mergeHeaders(init: RequestInit, jsonBody: boolean): HeadersInit {
  const headers: Record<string, string> = { ...CSRF_HEADERS };
  if (jsonBody) headers["Content-Type"] = "application/json";
  // Caller-supplied headers win — they may override Content-Type for
  // upload paths that send arbitrary MIME.
  Object.assign(headers, (init.headers as Record<string, string>) ?? {});
  return headers;
}

async function extractError(res: Response): Promise<Error> {
  // Try JSON first (FastAPI errors are { detail: string }); fall back
  // to the status line so we never throw with an opaque "Error: undefined".
  try {
    const data = await res.json();
    const detail = (data as { detail?: string }).detail;
    if (typeof detail === "string" && detail) return new Error(detail);
  } catch {
    /* response wasn't JSON */
  }
  return new Error(`status ${res.status}`);
}

export async function apiJson<T>(input: RequestInfo, init: RequestInit = {}): Promise<T> {
  const hasJsonBody = init.body !== undefined && typeof init.body === "string";
  const res = await fetch(input, {
    credentials: "same-origin",
    ...init,
    headers: mergeHeaders(init, hasJsonBody),
  });
  if (!res.ok) throw await extractError(res);
  // 204 / empty body — caller asked for T but there's nothing to parse.
  // Returning {} as T is what the legacy hand-rolled helpers did.
  if (res.status === 204) return {} as T;
  return (await res.json()) as T;
}

export async function apiVoid(input: RequestInfo, init: RequestInit = {}): Promise<void> {
  const hasJsonBody = init.body !== undefined && typeof init.body === "string";
  const res = await fetch(input, {
    credentials: "same-origin",
    ...init,
    headers: mergeHeaders(init, hasJsonBody),
  });
  if (!res.ok) throw await extractError(res);
}

export async function apiRaw(input: RequestInfo, init: RequestInit = {}): Promise<Response> {
  const hasJsonBody = init.body !== undefined && typeof init.body === "string";
  return fetch(input, {
    credentials: "same-origin",
    ...init,
    headers: mergeHeaders(init, hasJsonBody),
  });
}

// ─── /api/fs/config cache ─────────────────────────────────────────────
// File-panel + session-picker both need to know where the fs jail is
// rooted (otherwise they'd default to /home, which is the parent of
// the default root and gets 403'd by the jail check). Fetched once
// per SPA lifetime; the operator doesn't hot-swap CCPIPE_FS_ROOT.
export interface FsConfig {
  root: string;
  upload_limit_mb: number;
}

let _fsConfigPromise: Promise<FsConfig> | null = null;

export function getFsConfig(): Promise<FsConfig> {
  if (_fsConfigPromise === null) {
    _fsConfigPromise = apiJson<FsConfig>("/api/fs/config").catch((err) => {
      // Reset so a transient failure doesn't lock the cache to a
      // rejected promise; next caller retries.
      _fsConfigPromise = null;
      throw err;
    });
  }
  return _fsConfigPromise;
}

// ─── /api/fs/markdown-index ───────────────────────────────────────────
// Every Markdown file under a project root, for the toolbar "Docs"
// dropdown. Not cached — the tree changes as the user works, and it's a
// single cheap GET per open.
export interface MarkdownIndexEntry {
  name: string;
  path: string;   // absolute, for /view?path=
  rel: string;    // relative to root, for display
}
export interface MarkdownIndex {
  root: string;
  entries: MarkdownIndexEntry[];
  truncated: boolean;
}

export function listMarkdown(root: string): Promise<MarkdownIndex> {
  return apiJson<MarkdownIndex>(
    `/api/fs/markdown-index?root=${encodeURIComponent(root)}`,
  );
}

// ─── /api/mic/config ──────────────────────────────────────────────────
// Voice-input behaviour knobs (see backend MicConfig). Not cached —
// the settings modal mutates this and the mic streamer must see the
// change immediately. Each fetch is one cheap GET so a fresh round-
// trip per consumer is fine.
export interface MicConfig {
  auto_stop_enabled: boolean;
  silence_ms: number;
  drain_pad_ms: number;
  max_recording_seconds: number;
}

export function getMicConfig(): Promise<MicConfig> {
  return apiJson<MicConfig>("/api/mic/config");
}

export function setMicConfig(patch: Partial<MicConfig>): Promise<MicConfig> {
  return apiJson<MicConfig>("/api/mic/config", {
    method: "POST",
    body: JSON.stringify(patch),
  });
}
