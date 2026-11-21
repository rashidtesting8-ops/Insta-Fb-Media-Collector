import { NextResponse } from "next/server";
import { dashboardDenied, workerUrl, workerHeaders } from "@/lib/server";

export async function GET(_req: Request, ctx: { params: Promise<{ id: string }> }) {
  const denied = await dashboardDenied();
  if (denied) return denied;
  const { id } = await ctx.params;
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(id)) return NextResponse.json({ error: "Invalid job ID" }, { status: 400 });
  try {
    const res = await fetch(workerUrl(`/jobs/${id}/manifest`), { headers: workerHeaders(), cache: "no-store" });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ error: "Worker unavailable" }, { status: 502 });
  }
}
