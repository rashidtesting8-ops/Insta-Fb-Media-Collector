import { NextResponse } from "next/server";
import { workerUrl, workerHeaders } from "@/lib/server";

export async function GET(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  const url = new URL(req.url);
  const path = url.searchParams.get("path") || "";
  try {
    const res = await fetch(workerUrl(`/jobs/${id}/file?path=${encodeURIComponent(path)}`), { headers: workerHeaders() });
    if (!res.ok) return NextResponse.json({ error: res.statusText }, { status: res.status });
    const buf = await res.arrayBuffer();
    return new NextResponse(buf, {
      status: 200,
      headers: { "Content-Type": res.headers.get("content-type") || "application/octet-stream" },
    });
  } catch (e: unknown) {
    return NextResponse.json({ error: e instanceof Error ? e.message : String(e) }, { status: 502 });
  }
}
