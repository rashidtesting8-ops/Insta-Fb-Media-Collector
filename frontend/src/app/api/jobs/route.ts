import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { namedCall, namedCookie } from "@/lib/named";
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
    const body = await readObject(req);
    const named = (await cookies()).get(namedCookie)?.value;
    if (named) {
      const reservation = await namedCall("intake", body, named);
      if (!reservation.ok) return NextResponse.json({ error: "Named intake rejected" }, { status: reservation.status });
      const ticket = (await reservation.json()).idempotency_key;
      if (typeof ticket !== "string" || !/^named-[a-f0-9]{64}$/.test(ticket)) throw new Error();
      body.idempotency_key = ticket;
    } else if (String(body.idempotency_key || "").trim().startsWith("named-")) {
      return NextResponse.json({ error: "Reserved intake key" }, { status: 400 });
    }
    const result = await submitIntake(body, n8nUrl("/"), token, fetch, process.env.N8N_INTAKE_PATH || undefined);
    console.info(JSON.stringify({ event: "dashboard_intake", status: result.status }));
    return NextResponse.json(result.body, { status: result.status });
  } catch (error) {
    if (error instanceof BodyError) return NextResponse.json({ error: error.message }, { status: error.status });
    console.warn(JSON.stringify({ event: "dashboard_intake", status: 502 }));
    return NextResponse.json({ error: "Pipeline intake unavailable" }, { status: 502 });
  }
}
