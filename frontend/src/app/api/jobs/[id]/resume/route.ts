import { cookies } from "next/headers";
import { namedCall, namedCookieName } from "@/lib/named";
import { dashboardOrigin } from "@/lib/security";

const json = (data: unknown, status: number) =>
  Response.json(data, { status, headers: { "Cache-Control": "private, no-store" } });

export async function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  if (!/^[a-f0-9]{32}$/.test(id)) return json({ error: "Invalid job ID" }, 400);
  if (!dashboardOrigin() || req.headers.get("origin") !== dashboardOrigin())
    return json({ error: "Origin denied" }, 403);
  const session = (await cookies()).get(namedCookieName())?.value;
  if (!session) return json({ error: "Named Admin sign-in required to resume collection" }, 401);
  try {
    const upstream = await namedCall("collection-resume", { job_id: id }, session, 10000);
    const data = await upstream.json().catch(() => ({}));
    if (!upstream.ok) {
      return json(
        { error: typeof data.detail === "string" ? data.detail : "Unable to resume collection for this job." },
        upstream.status,
      );
    }
    return json(data, upstream.status);
  } catch {
    return json({ error: "Resume collection request unavailable" }, 503);
  }
}
