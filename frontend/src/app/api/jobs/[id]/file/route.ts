import { NextResponse } from "next/server";
import { dashboardDenied, workerUrl, workerHeaders } from "@/lib/server";

export async function GET(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const denied = await dashboardDenied();
  if (denied) return denied;
  const { id } = await ctx.params;
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(id)) return NextResponse.json({ error: "Invalid job ID" }, { status: 400 });
  const url = new URL(req.url);
  const path = url.searchParams.get("path") || "";
  try {
    const res = await fetch(workerUrl(`/jobs/${id}/file?path=${encodeURIComponent(path)}`), { headers: workerHeaders() });
    if (!res.ok) return NextResponse.json({ error: res.statusText }, { status: res.status });
    const type = (res.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
    if (!["image/jpeg", "image/png", "image/webp", "image/gif", "image/avif", "video/mp4", "video/webm", "video/quicktime"].includes(type)) {
      await res.body?.cancel();
      return NextResponse.json({ error: "Unsupported preview type" }, { status: 415 });
    }
    const headers: Record<string, string> = { "Content-Type": type, "X-Content-Type-Options": "nosniff" };
    const length = res.headers.get("content-length");
    if (length) headers["Content-Length"] = length;
    return new NextResponse(res.body, {
      status: 200,
      headers,
    });
  } catch {
    return NextResponse.json({ error: "Worker unavailable" }, { status: 502 });
  }
}
