import { createHash, createHmac, randomUUID, timingSafeEqual } from "node:crypto";

const SESSION_SECONDS = 8 * 60 * 60;
const rates = {
  login: { limit: 10, times: [] as number[] },
  submit: { limit: 20, times: [] as number[] },
  setup: { limit: 5, times: [] as number[] },
};

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

function isLoopbackIp(raw: string): boolean {
  const value = raw.trim().toLowerCase().replace(/^\[|\]$/g, "");
  if (value === "::1" || value === "127.0.0.1" || value === "::ffff:127.0.0.1") return true;
  const ipv4 = value.startsWith("::ffff:") ? value.slice(7) : value;
  return /^127\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)$/.test(ipv4);
}

export function isLoopbackOrigin(origin: string | undefined): boolean {
  if (!origin) return false;
  try {
    const url = new URL(origin);
    return origin === url.origin && url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname);
  } catch {
    return false;
  }
}

export function isLoopbackHost(host: string | null): boolean {
  if (!host) return false;
  return /^(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?$/i.test(host.trim());
}

/**
 * Returns true if request headers indicate a non-loopback reverse proxy or remote peer.
 * Next.js standalone internally attaches loopback x-forwarded-for / x-forwarded-host for
 * direct local connections; every hop in x-forwarded-for and x-real-ip must be loopback,
 * x-forwarded-host must be a loopback host, and external CDN/proxy headers are always denied.
 */
export function hasForwardingHeaders(headers: Headers): boolean {
  for (const name of ["forwarded", "cf-connecting-ip", "true-client-ip"]) {
    const value = headers.get(name);
    if (value !== null && value.trim().length > 0) return true;
  }
  const xff = headers.get("x-forwarded-for");
  if (xff !== null && xff.trim().length > 0) {
    const hops = xff.split(",").map((s) => s.trim()).filter(Boolean);
    if (hops.length === 0 || hops.some((hop) => !isLoopbackIp(hop))) return true;
  }
  const realIp = headers.get("x-real-ip");
  if (realIp !== null && realIp.trim().length > 0 && !isLoopbackIp(realIp)) return true;
  const xfh = headers.get("x-forwarded-host");
  if (xfh !== null && xfh.trim().length > 0) {
    const hosts = xfh.split(",").map((s) => s.trim()).filter(Boolean);
    if (hosts.length === 0 || hosts.some((h) => !isLoopbackHost(h))) return true;
  }
  return false;
}

export function dashboardOrigin() {
  const raw = process.env.DASHBOARD_ORIGIN;
  if (!raw) return;
  try {
    const url = new URL(raw);
    if (raw === url.origin && (url.protocol === "https:" ||
        (url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname)))) return raw;
  } catch {
    return;
  }
}

export function cookieName() {
  return dashboardOrigin()?.startsWith("http:") ? "pgm_session" : "__Host-pgm_session";
}

export function cookieOptions(maxAge = SESSION_SECONDS) {
  return { httpOnly: true, secure: cookieName().startsWith("__Host-"), sameSite: "strict" as const, path: "/", maxAge };
}

function equal(left: string, right: string) {
  return timingSafeEqual(createHash("sha256").update(left).digest(), createHash("sha256").update(right).digest());
}

function signingKey() {
  const key = process.env.DASHBOARD_SESSION_SECRET;
  const password = process.env.DASHBOARD_PASSWORD;
  return key && key.length >= 32 && password && key !== password && dashboardOrigin() ? key : undefined;
}

function signature(value: string, key: string) {
  return createHmac("sha256", key).update(value).digest("base64url");
}

export function issueSession(password: string, now = Date.now()) {
  const key = signingKey();
  if (!key || !equal(password, process.env.DASHBOARD_PASSWORD!)) return;
  const value = `${Math.floor(now / 1000) + SESSION_SECONDS}.${randomUUID()}`;
  return `${value}.${signature(value, key)}`;
}

export function validSession(token: string | undefined, now = Date.now()) {
  const key = signingKey();
  if (!key || !token || !/^\d{10}\.[\da-f-]{36}\.[\w-]{43}$/.test(token)) return false;
  const [expires, nonce, mac] = token.split(".");
  return Number(expires) > Math.floor(now / 1000) && equal(mac, signature(`${expires}.${nonce}`, key));
}

export function admitRequest(kind: keyof typeof rates, now = Date.now()) {
  const { times, limit } = rates[kind];
  while (times.length && times[0] <= now - 60_000) times.shift();
  if (times.length >= limit) return false;
  times.push(now);
  return true;
}

export class BodyError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function readObject(req: Request, limit = 16 * 1024): Promise<Record<string, unknown>> {
  if (req.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json") {
    throw new BodyError("Expected application/json", 415);
  }
  const length = req.headers.get("content-length");
  if (length !== null && !/^\d+$/.test(length)) throw new BodyError("Invalid Content-Length", 400);
  if (Number(length) > limit) throw new BodyError("Request too large", 413);
  const reader = req.body?.getReader();
  if (!reader) throw new BodyError("Expected a JSON object", 400);
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let size = 0;
  let text = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        void reader.cancel().catch(() => {});
        throw new BodyError("Request too large", 413);
      }
      text += decoder.decode(value, { stream: true });
    }
    const data = JSON.parse(text + decoder.decode());
    if (data === null || typeof data !== "object" || Array.isArray(data)) throw new Error();
    return data;
  } catch (error) {
    if (error instanceof BodyError) throw error;
    throw new BodyError("Expected a JSON object", 400);
  } finally {
    reader.releaseLock();
  }
}
