import { NextResponse } from "next/server";
import { dashboardDenied, workerUrl, n8nUrl } from "@/lib/server";

export async function GET() {
  const denied = await dashboardDenied();
  if (denied) return denied;
  const result: Record<string, unknown> = { worker: { ok: false }, n8n: { ok: false }, ai: { configured: false }, drive: { configured: false } };
  try {
    const res = await fetch(workerUrl("/ready"), { cache: "no-store", signal: AbortSignal.timeout(2500) });
    result.worker = { ok: res.ok, status: res.status };
  } catch { result.worker = { ok: false }; }
  try {
    const res = await fetch(n8nUrl("/healthz/readiness"), { cache: "no-store", signal: AbortSignal.timeout(2500) });
    result.n8n = { ok: res.ok, status: res.status };
  } catch { result.n8n = { ok: false }; }
  // AI/drive readiness inferred from env (server-side only, no secrets exposed)
  result.ai = { configured: !!process.env.AI_API_KEY && !!process.env.AI_PROVIDER };
  result.drive = { configured: !!process.env.GOOGLE_DRIVE_READY };
  return NextResponse.json(result);
}
