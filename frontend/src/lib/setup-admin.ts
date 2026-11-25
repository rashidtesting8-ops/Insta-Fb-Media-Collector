import { namedCall } from "./named";
import {
  admitRequest,
  BodyError,
  dashboardOrigin,
  hasForwardingHeaders,
  isLoopbackHost,
  isLoopbackOrigin,
  readObject,
} from "./security";

export const SETUP_CSRF_HEADER = "x-requested-with";
export const SETUP_CSRF_VALUE = "pgm-setup-admin";
export const MIN_SETUP_PASSWORD_LENGTH = 12;

export type NamedCaller = (
  action: string,
  body: Record<string, unknown>,
  session?: string,
  timeoutMs?: number,
) => Promise<Response>;

export interface SetupResponse {
  status: number;
  body: Record<string, unknown>;
  headers?: Record<string, string>;
}

const NO_STORE = { "Cache-Control": "no-store" };

function checkLoopback(req: Request): { ok: true; origin: string } | { ok: false; response: SetupResponse } {
  const origin = dashboardOrigin();
  if (!isLoopbackOrigin(origin) || !origin) {
    return { ok: false, response: { status: 403, body: { error: "Local loopback setup is disabled" }, headers: NO_STORE } };
  }
  if (!isLoopbackHost(req.headers.get("host")) || hasForwardingHeaders(req.headers)) {
    return { ok: false, response: { status: 403, body: { error: "Loopback access required" }, headers: NO_STORE } };
  }
  return { ok: true, origin };
}

function sanitizeErrorDetail(raw: unknown, secrets: string[], fallback: string): string {
  if (typeof raw !== "string") return fallback;
  const trimmed = raw.trim();
  if (!trimmed || trimmed.length > 160) return fallback;
  for (const secret of secrets) {
    if (secret && trimmed.includes(secret)) return fallback;
  }
  return trimmed;
}

export async function handleSetupStatus(
  req: Request,
  callNamed: NamedCaller = namedCall,
): Promise<SetupResponse> {
  const local = checkLoopback(req);
  if (!local.ok) return local.response;
  try {
    const res = await callNamed("bootstrap-status", {}, "", 5000);
    if (!res.ok) return { status: 503, body: { available: false, error: "Setup status unavailable" }, headers: NO_STORE };
    const data = await res.json();
    return { status: 200, body: { available: Boolean(data?.available) }, headers: NO_STORE };
  } catch {
    return { status: 503, body: { available: false, error: "Setup status unavailable" }, headers: NO_STORE };
  }
}

export async function handleSetupSubmit(
  req: Request,
  callNamed: NamedCaller = namedCall,
  now = Date.now(),
): Promise<SetupResponse> {
  const local = checkLoopback(req);
  if (!local.ok) return local.response;

  // Strict CSRF enforcement: exact loopback Origin, explicit custom header, and same-origin Fetch Metadata when present.
  if (req.headers.get("origin") !== local.origin) {
    return { status: 403, body: { error: "Origin denied" }, headers: NO_STORE };
  }
  if (req.headers.get(SETUP_CSRF_HEADER) !== SETUP_CSRF_VALUE) {
    return { status: 403, body: { error: "CSRF validation failed" }, headers: NO_STORE };
  }
  const fetchSite = req.headers.get("sec-fetch-site");
  if (fetchSite && fetchSite.toLowerCase() !== "same-origin") {
    return { status: 403, body: { error: "Cross-site request denied" }, headers: NO_STORE };
  }

  if (!admitRequest("setup", now)) {
    return {
      status: 429,
      body: { error: "Too many setup attempts; try again later" },
      headers: { ...NO_STORE, "Retry-After": "60" },
    };
  }

  let payload: Record<string, unknown>;
  try {
    payload = await readObject(req, 4096);
  } catch (err) {
    const status = err instanceof BodyError ? err.status : 400;
    const msg = err instanceof BodyError ? err.message : "Invalid setup payload";
    return { status, body: { error: msg }, headers: NO_STORE };
  }

  const keys = Object.keys(payload);
  const allowed = new Set(["setup_token", "name", "password", "confirm_password"]);
  if (keys.length !== allowed.size || keys.some((k) => !allowed.has(k))) {
    return { status: 400, body: { error: "Invalid setup fields" }, headers: NO_STORE };
  }

  const setup_token = typeof payload.setup_token === "string" ? payload.setup_token.trim() : "";
  const name = typeof payload.name === "string" ? payload.name.trim() : "";
  const password = typeof payload.password === "string" ? payload.password : "";
  const confirm_password = typeof payload.confirm_password === "string" ? payload.confirm_password : "";

  if (!setup_token) {
    return { status: 403, body: { error: "Setup token is required" }, headers: NO_STORE };
  }
  if (!name) {
    return { status: 400, body: { error: "Account name is required" }, headers: NO_STORE };
  }

  try {
    // Forward to worker so bootstrap-consumed (409) and single-use token validation (403)
    // run atomically before password rules and Argon2id hashing.
    const res = await callNamed(
      "bootstrap",
      { setup_token, name, password, confirm_password },
      "",
      15_000,
    );
    const data = await res.json().catch(() => ({}));
    console.info(JSON.stringify({ event: "named_browser_bootstrap", status: res.status, success: res.ok }));
    if (!res.ok) {
      const fallback =
        res.status === 409
          ? "Initial setup has already been completed"
          : res.status === 403
            ? "Invalid or expired setup token"
            : "Setup request rejected";
      const safeError = sanitizeErrorDetail(
        (data as Record<string, unknown>)?.detail ?? (data as Record<string, unknown>)?.error,
        [setup_token, password, confirm_password],
        fallback,
      );
      return { status: res.status, body: { error: safeError }, headers: NO_STORE };
    }
    const principal_id = typeof (data as Record<string, unknown>)?.principal_id === "string"
      ? (data as Record<string, unknown>).principal_id
      : undefined;
    if (!principal_id) {
      return { status: 502, body: { error: "Invalid bootstrap response" }, headers: NO_STORE };
    }
    return { status: 201, body: { ok: true, principal_id }, headers: NO_STORE };
  } catch {
    return { status: 503, body: { error: "Setup service unavailable" }, headers: NO_STORE };
  }
}
