/* eslint-disable @typescript-eslint/no-explicit-any */
import { NextResponse } from "next/server";
import { workerUrl, n8nUrl, workerHeaders } from "@/lib/server";

export async function GET() {
  const result: Record<string, unknown> = { worker: { ok: false }, n8n: { ok: false }, ai: { configured: false }, drive: { configured: false } };
  try {
    const res = await fetch(workerUrl("/health"), { cache: "no-store" });
    result.worker = { ok: res.ok, status: res.status };
  } catch (e: unknown) { result.worker = { ok: false, error: (e instanceof Error ? e.message : String(e)) }; }
  try {
    const res = await fetch(n8nUrl("/healthz"), { cache: "no-store" });
    result.n8n = { ok: res.ok, status: res.status };
  } catch (e: unknown) { result.n8n = { ok: false, error: (e instanceof Error ? e.message : String(e)) }; }
  // AI/drive readiness inferred from env (server-side only, no secrets exposed)
  result.ai = { configured: !!process.env.AI_API_KEY && !!process.env.AI_PROVIDER };
  result.drive = { configured: !!process.env.GOOGLE_DRIVE_READY };
  return NextResponse.json(result);
}
