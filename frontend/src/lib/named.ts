import { createHash, createHmac, randomUUID } from "node:crypto";

export const namedCookie = "__Host-pgm_named";
export const namedCookieOptions = {
  secure: true, httpOnly: true, sameSite: "strict" as const, path: "/", maxAge: 8 * 3600,
};

export async function namedCall(action: string, body: Record<string, unknown>, session = "") {
  const key = process.env.NAMED_ASSERTION_SECRET;
  const dashboard = process.env.MEDIA_WORKER_DASHBOARD_TOKEN;
  if (!key || key.length < 32 || !dashboard || [
    dashboard, process.env.MEDIA_WORKER_TOKEN, process.env.DASHBOARD_PASSWORD,
    process.env.DASHBOARD_SESSION_SECRET, process.env.N8N_PIPELINE_TOKEN,
  ].includes(key)) throw new Error("Named authentication unavailable");
  const raw = JSON.stringify(body);
  const hash = (s: string) => createHash("sha256").update(s).digest("hex");
  const claims = Buffer.from(JSON.stringify({
    iss: "pgm-frontend", aud: "pgm-worker", sub: hash(session), action, body: hash(raw),
    exp: Math.floor(Date.now()/1000) + 30, jti: randomUUID().replaceAll("-", ""),
  })).toString("base64url");
  const signature = createHmac("sha256", key).update(claims).digest("hex");
  const url = new URL("/named/" + action, process.env.WORKER_URL || "http://127.0.0.1:8788");
  return fetch(url, {
    method: "POST", cache: "no-store", redirect: "error", signal: AbortSignal.timeout(5000),
    headers: { "Content-Type": "application/json", "X-API-Key": dashboard,
      "X-Named-Session": session, "X-Named-Assertion": claims + "." + signature },
    body: raw,
  });
}
