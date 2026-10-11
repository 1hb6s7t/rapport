/** Fetch wrapper: same-origin, cookie session, CSRF header for state changes. */

const CSRF_KEY = "rapport_csrf";
let memoryCsrf: string | null = null;

export function getCsrf(): string | null {
  try {
    return sessionStorage.getItem(CSRF_KEY) || memoryCsrf;
  } catch {
    return memoryCsrf;
  }
}
export function setCsrf(v: string | null) {
  memoryCsrf = v;
  try {
    if (v) sessionStorage.setItem(CSRF_KEY, v);
    else sessionStorage.removeItem(CSRF_KEY);
  } catch {
    /* ignore */
  }
}

export class ApiError extends Error {
  code: string;
  status: number;
  hint: string;
  constructor(status: number, code: string, msg: string, hint = "") {
    super(msg);
    this.status = status;
    this.code = code;
    this.hint = hint;
  }
}

let localSession: Promise<void> | null = null;
export function connectLocalSession(): Promise<void> {
  if (!localSession) {
    localSession = (async () => {
      const response = await fetch("/api/session", { method: "POST", credentials: "same-origin", headers: { "x-rapport-local": "1" } });
      const result = await response.json();
      if (!response.ok) throw new ApiError(response.status, result.error?.code || "local_connection", result.error?.msg || "连接本机服务失败");
      setCsrf(result.csrf);
    })().finally(() => { localSession = null; });
  }
  return localSession;
}

async function request<T>(method: string, path: string, body?: unknown, isForm = false, reconnect = true): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") {
    const csrf = getCsrf();
    if (csrf) headers["x-rapport-csrf"] = csrf;
  }
  if (body !== undefined && !isForm) headers["content-type"] = "application/json";
  const res = await fetch(`/api${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
  });
  const ct = res.headers.get("content-type") || "";
  if (!res.ok) {
    let code = "http_error";
    let msg = `${res.status}`;
    let hint = "";
    if (ct.includes("json")) {
      const j = await res.json().catch(() => ({}));
      code = j?.error?.code ?? code;
      msg = j?.error?.msg ?? msg;
      hint = j?.error?.hint ?? "";
    }
    // These two codes are emitted by the authentication dependency BEFORE the
    // handler runs. Never replay network failures or uncertain send outcomes.
    if (reconnect && ((res.status === 401 && code === "unpaired") || (res.status === 403 && code === "csrf"))) {
      await connectLocalSession();
      return request<T>(method, path, body, isForm, false);
    }
    if (res.status === 401) setCsrf(null);
    throw new ApiError(res.status, code, msg, hint);
  }
  if (ct.includes("json")) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

export const api = {
  get: <T>(p: string) => request<T>("GET", p),
  post: <T>(p: string, b?: unknown) => request<T>("POST", p, b),
  put: <T>(p: string, b?: unknown) => request<T>("PUT", p, b),
  patch: <T>(p: string, b?: unknown) => request<T>("PATCH", p, b),
  del: <T>(p: string) => request<T>("DELETE", p),
  form: <T>(p: string, f: FormData) => request<T>("POST", p, f, true),
};
