import { NextResponse } from "next/server";
import { dashboardDenied, n8nUrl, workerUrl, workerHeaders } from "@/lib/server";
import { admitRequest, BodyError, readObject } from "@/lib/security";
import { submitIntake } from "@/lib/intake";

export async function GET() {
  const denied = await dashboardDenied();
  if (denied) return denied;
  try {
    const res = await fetch(workerUrl("/jobs"), { headers: workerHeaders(), cache: "no-store" });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ error: "Worker unavailable" }, { status: 502 });
  }
}

export async function POST(req: Request) {
  const denied = await dashboardDenied(req);
  if (denied) return denied;
  if (!admitRequest("submit")) {
    return NextResponse.json({ error: "Submission limit reached" }, { status: 429, headers: { "Retry-After": "60" } });
  }
  const token = process.env.N8N_PIPELINE_TOKEN;
  if (!token) return NextResponse.json({ error: "Pipeline intake unavailable" }, { status: 503 });
  try {
    const result = await submitIntake(await readObject(req), n8nUrl("/"), token);
    console.info(JSON.stringify({ event: "dashboard_intake", status: result.status }));
    return NextResponse.json(result.body, { status: result.status });
  } catch (error) {
    if (error instanceof BodyError) return NextResponse.json({ error: error.message }, { status: error.status });
    console.warn(JSON.stringify({ event: "dashboard_intake", status: 502 }));
    return NextResponse.json({ error: "Pipeline intake unavailable" }, { status: 502 });
  }
}
