import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { namedCall, namedCookieName } from "@/lib/named";
import { BodyError, dashboardOrigin, readObject } from "@/lib/security";
import { dashboardDenied, n8nUrl, workerUrl, workerHeaders } from "@/lib/server";

export async function GET(_req: Request, ctx: { params: Promise<{ id: string }> }) {
  const denied = await dashboardDenied();
  if (denied) return denied;
  const { id } = await ctx.params;
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(id)) return NextResponse.json({ error: "Invalid job ID" }, { status: 400 });
  try {
    const res = await fetch(workerUrl(`/jobs/${id}/delivery`), { headers: workerHeaders(), cache: "no-store" });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ error: "Worker unavailable" }, { status: 502 });
  }
}

export async function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  if (!/^[a-f0-9]{32}$/.test(id)) {
    return NextResponse.json({ error: "Invalid job ID" }, { status: 400 });
  }
  if (!dashboardOrigin() || req.headers.get("origin") !== dashboardOrigin()) {
    return NextResponse.json({ error: "Origin denied" }, { status: 403 });
  }
  const session = (await cookies()).get(namedCookieName())?.value;
  if (!session) {
    return NextResponse.json({ error: "Named Admin sign-in required for Drive delivery" }, { status: 401 });
  }
  const deliveryToken = process.env.N8N_ADMIN_WEBHOOK_TOKEN;
  if (!deliveryToken) {
    return NextResponse.json(
      { error: "Drive delivery webhook token not configured (N8N_ADMIN_WEBHOOK_TOKEN)" },
      { status: 503 },
    );
  }
  try {
    const body = await readObject(req, 4096);
    if (body.authorization_confirmed !== true) {
      return NextResponse.json({ error: "Explicit commercial release confirmation required" }, { status: 400 });
    }
    const businessName =
      typeof body.business_name === "string" && body.business_name.trim()
        ? body.business_name.trim().slice(0, 120)
        : "Pet Grooming Business";

    const verifiedSession = await namedCall("session", {}, session);
    if (!verifiedSession.ok) {
      return NextResponse.json({ error: "Named Admin session expired or invalid" }, { status: 401 });
    }

    const headerName = process.env.N8N_ADMIN_WEBHOOK_HEADER || "X-Admin-Key";
    const upstream = await fetch(n8nUrl("/webhook/drive-delivery"), {
      method: "POST",
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(60000),
      headers: {
        "Content-Type": "application/json",
        [headerName]: deliveryToken,
      },
      body: JSON.stringify({
        job_id: id,
        business_name: businessName,
        authorization_confirmed: true,
      }),
    });
    const data = await upstream.json().catch(() => ({ error: "Invalid delivery response from n8n" }));
    return NextResponse.json(data, {
      status: upstream.status,
      headers: { "Cache-Control": "private, no-store" },
    });
  } catch (error) {
    if (error instanceof BodyError) {
      return NextResponse.json({ error: error.message }, { status: error.status });
    }
    return NextResponse.json({ error: "Google Drive delivery trigger unavailable" }, { status: 502 });
  }
}
