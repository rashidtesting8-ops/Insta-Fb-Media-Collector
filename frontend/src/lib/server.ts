import { cookies } from "next/headers";
import { cookieName, dashboardOrigin, validSession } from "./security";

export function workerUrl(path: string): string {
  const base = process.env.WORKER_URL || "http://127.0.0.1:8788";
  return `${base.replace(/\/$/, "")}${path}`;
}

export function n8nUrl(path: string): string {
  const base = process.env.N8N_URL || "http://127.0.0.1:5679";
  return `${base.replace(/\/$/, "")}${path}`;
}

export function workerHeaders(): Record<string, string> {
  const h: Record<string, string> = {};
  if (process.env.MEDIA_WORKER_DASHBOARD_TOKEN) h["X-API-Key"] = process.env.MEDIA_WORKER_DASHBOARD_TOKEN;
  return h;
}

export async function dashboardDenied(req?: Request): Promise<Response | undefined> {
  if (req && req.method === "POST" &&
      (!dashboardOrigin() || req.headers.get("origin") !== dashboardOrigin())) {
    return Response.json({ error: "Origin denied" }, { status: 403 });
  }
  if (!validSession((await cookies()).get(cookieName())?.value)) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }
}
