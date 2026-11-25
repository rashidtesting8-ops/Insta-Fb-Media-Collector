import { cookies } from "next/headers";
import { namedCall, namedCookieName } from "@/lib/named";
import { BodyError, dashboardOrigin, readObject } from "@/lib/security";

const json = (data: unknown, status: number) =>
  Response.json(data, { status, headers: { "Cache-Control": "private, no-store" } });

export async function GET(_req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  if (!/^[a-f0-9]{32}$/.test(id)) return json({ error: "Invalid job ID" }, 400);
  const session = (await cookies()).get(namedCookieName())?.value;
  if (!session) return json({ error: "Named Admin sign-in required" }, 401);
  try {
    const upstream = await namedCall("collection-rights-review", { job_id: id }, session);
    if (!upstream.ok) {
      return json({ error: "Rights ledger unavailable for this job or session." }, upstream.status);
    }
    const data = await upstream.json();
    if (data.job_id !== id) return json({ error: "Worker job mismatch" }, 502);
    return json(data, 200);
  } catch {
    return json({ error: "Rights ledger request unavailable" }, 503);
  }
}

export async function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  if (!/^[a-f0-9]{32}$/.test(id)) return json({ error: "Invalid job ID" }, 400);
  if (!dashboardOrigin() || req.headers.get("origin") !== dashboardOrigin())
    return json({ error: "Origin denied" }, 403);
  const session = (await cookies()).get(namedCookieName())?.value;
  if (!session) return json({ error: "Named Admin sign-in required" }, 401);
  const match = req.headers.get("if-match")?.match(/^"([a-f0-9]{64})"$/);
  const key = req.headers.get("idempotency-key");
  if (!match || !key || !/^[\w-]{1,128}$/.test(key)) {
    return json({ error: "Revision ETag and request key required" }, 400);
  }
  try {
    const body = await readObject(req, 4096);
    const { asset_id, decision, reason, evidence_ref } = body;
    if (
      Object.keys(body).length !== 4 ||
      typeof asset_id !== "string" || !asset_id.trim() ||
      typeof decision !== "string" || !["verified", "rejected", "revoked"].includes(decision) ||
      typeof reason !== "string" || !reason.trim() ||
      typeof evidence_ref !== "string" || !evidence_ref.trim()
    ) {
      return json({ error: "asset_id, decision (verified/rejected/revoked), reason, and evidence_ref are required" }, 400);
    }
    const upstream = await namedCall(
      "collection-rights-decide",
      {
        job_id: id,
        asset_id: asset_id.trim(),
        decision,
        reason: reason.trim(),
        evidence_ref: evidence_ref.trim(),
        if_match: match[1],
        request_key: key,
      },
      session,
    );
    const data = await upstream.json().catch(() => ({}));
    if (!upstream.ok) {
      return json(
        {
          error:
            upstream.status === 412
              ? "Review changed. Refresh before recording rights."
              : typeof data.detail === "string"
                ? data.detail
                : "Rights decision rejected.",
        },
        upstream.status,
      );
    }
    return json(data, 200);
  } catch (error) {
    return json(
      { error: "Rights decision request unavailable" },
      error instanceof BodyError ? error.status : 503,
    );
  }
}
