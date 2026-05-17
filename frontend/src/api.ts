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
