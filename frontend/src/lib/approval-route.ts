import { cookies } from "next/headers";
import { namedCall, namedCookieName } from "./named";
import { BodyError, dashboardOrigin, readObject } from "./security";

type Context = { params: Promise<{ id: string }> };
type Operation = "panel" | "decision" | "start" | "website-policy";
const response = (data: unknown, status: number) => Response.json(data, {
  status, headers: { "Cache-Control": "private, no-store" },
});

export async function approvalRoute(req: Request, ctx: Context, op: Operation) {
  const { id } = await ctx.params;
  if (!/^[a-f0-9]{32}$/.test(id)) return response({ error: "Invalid job ID" }, 400);
  if (op !== "panel" && (!dashboardOrigin() || req.headers.get("origin") !== dashboardOrigin()))
    return response({ error: "Origin denied" }, 403);
  const session = (await cookies()).get(namedCookieName())?.value;
  if (!session) return response({ error: "Named Admin sign-in required" }, 401);
  try {
    let action: string = op;
    const data: Record<string, unknown> = { job_id: id };
    if (op !== "panel") {
      const body = await readObject(req, 4096);
      const match = req.headers.get("if-match")?.match(/^"([a-f0-9]{64})"$/);
      const key = req.headers.get("idempotency-key");
      if (!match || !key || !/^[\w-]{1,128}$/.test(key)) return response({ error: "Revision and request key required" }, 400);
      data.if_match = match[1];
      data.request_key = key;
      if (op === "decision") {
        if (Object.keys(body).length !== 2 || typeof body.action !== "string" ||
            !["approve", "deny", "revoke"].includes(body.action) ||
            typeof body.reason !== "string" || !body.reason.trim())
          return response({ error: "Decision and reason required" }, 400);
        action = body.action;
        data.reason = body.reason.trim();
      } else if (op === "website-policy") {
        if (Object.keys(body).length !== 2 || typeof body.include_undated_website !== "boolean" ||
            typeof body.reason !== "string" || !body.reason.trim())
          return response({ error: "Website policy flag and reason required" }, 400);
        data.include_undated_website = body.include_undated_website;
        data.reason = body.reason.trim();
      } else if (Object.keys(body).length) return response({ error: "Unexpected Start AI fields" }, 400);
    }
    const upstream = await namedCall("collection-" + action, data, session, op === "website-policy" ? 15000 : 5000);
    if (!upstream.ok) return response({ error: upstream.status === 412 ? "Review changed. Refresh before deciding." :
      "Collection review unavailable for this job or session." }, upstream.status);
    if (op !== "panel") return response({ ok: true }, 200);
    const panel = await upstream.json();
    if (panel.job_id !== id) return response({ error: "Worker job mismatch" }, 502);
    if (new URL(req.url).searchParams.get("export") === "audit")
      return Response.json({ job_id: id, history: panel.history }, { headers: {
        "Cache-Control": "private, no-store", "Content-Disposition": `attachment; filename="approval-${id}.json"`,
      } });
    return response(panel, 200);
  } catch (error) {
    return response({ error: "Collection review request unavailable" }, error instanceof BodyError ? error.status : 503);
  }
}
