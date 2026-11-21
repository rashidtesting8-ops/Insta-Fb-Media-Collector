import { NextResponse } from "next/server";
import { dashboardDenied, workerUrl, workerHeaders } from "@/lib/server";
import { BodyError, readObject } from "@/lib/security";

export async function POST(req: Request) {
  const denied = await dashboardDenied(req);
  if (denied) return denied;
  try {
    const body = await readObject(req);
    const res = await fetch(workerUrl("/intake/validate"), {
      method: "POST",
      headers: { "Content-Type": "application/json", ...workerHeaders() },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    if (error instanceof BodyError) return NextResponse.json({ error: error.message }, { status: error.status });
    return NextResponse.json({ error: "Validation unavailable" }, { status: 502 });
  }
}
