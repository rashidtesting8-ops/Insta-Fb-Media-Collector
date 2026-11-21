import { NextResponse } from "next/server";
import { workerUrl, workerHeaders } from "@/lib/server";

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const res = await fetch(workerUrl("/intake/validate"), {
      method: "POST",
      headers: { "Content-Type": "application/json", ...workerHeaders() },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (e: unknown) {
    return NextResponse.json({ error: e instanceof Error ? e.message : String(e) }, { status: 502 });
  }
}
