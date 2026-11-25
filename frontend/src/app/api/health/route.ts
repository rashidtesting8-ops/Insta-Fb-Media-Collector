import { NextResponse } from "next/server";
import { dashboardDenied, workerUrl, workerHeaders, n8nUrl } from "@/lib/server";

export async function GET() {
  const denied = await dashboardDenied();
  if (denied) return denied;
  const result: {
    worker: { ok: boolean; status?: number; collection_engine?: string; m4_approval_enabled?: boolean };
    n8n: { ok: boolean; status?: number };
    ai: { configured: boolean; provider?: string | null; model?: string | null };
    drive: { configured: boolean; delivery_webhook_configured: boolean };
  } = {
    worker: { ok: false },
    n8n: { ok: false },
    ai: { configured: false, provider: null, model: null },
    drive: {
      configured: process.env.GOOGLE_DRIVE_READY === "1" || process.env.GOOGLE_DRIVE_READY === "true",
      delivery_webhook_configured: !!process.env.N8N_ADMIN_WEBHOOK_TOKEN,
    },
  };
  try {
    const res = await fetch(workerUrl("/ready"), { cache: "no-store", signal: AbortSignal.timeout(2500) });
    result.worker = { ok: res.ok, status: res.status };
  } catch {
    result.worker = { ok: false };
  }
  if (result.worker.ok) {
    try {
      const statusRes = await fetch(workerUrl("/status"), {
        headers: workerHeaders(),
        cache: "no-store",
        signal: AbortSignal.timeout(2500),
      });
      if (statusRes.ok) {
        const data = await statusRes.json();
        result.worker.collection_engine = typeof data.collection_engine === "string" ? data.collection_engine : undefined;
        result.worker.m4_approval_enabled = typeof data.m4_approval_enabled === "boolean" ? data.m4_approval_enabled : undefined;
        result.ai = {
          configured: data.ai_configured === true,
          provider: typeof data.ai_provider === "string" ? data.ai_provider : null,
          model: typeof data.ai_model === "string" ? data.ai_model : null,
        };
      }
    } catch {
      // Preserve honest ai.configured = false if worker status is unreachable
    }
  }
  try {
    const res = await fetch(n8nUrl("/healthz/readiness"), { cache: "no-store", signal: AbortSignal.timeout(2500) });
    result.n8n = { ok: res.ok, status: res.status };
  } catch {
    result.n8n = { ok: false };
  }
  return NextResponse.json(result, { headers: { "Cache-Control": "private, no-store" } });
}
