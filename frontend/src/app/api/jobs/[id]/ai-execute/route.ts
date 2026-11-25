import { cookies } from "next/headers";
import { namedCall, namedCookieName } from "@/lib/named";
import { BodyError, dashboardOrigin, readObject } from "@/lib/security";
import { workerHeaders, workerUrl } from "@/lib/server";

const json = (data: unknown, status: number) =>
  Response.json(data, { status, headers: { "Cache-Control": "private, no-store" } });

export async function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  if (!/^[a-f0-9]{32}$/.test(id)) return json({ error: "Invalid job ID" }, 400);
  if (!dashboardOrigin() || req.headers.get("origin") !== dashboardOrigin())
    return json({ error: "Origin denied" }, 403);
  const session = (await cookies()).get(namedCookieName())?.value;
  if (!session) return json({ error: "Named Admin sign-in required" }, 401);
  const match = req.headers.get("if-match")?.match(/^"([a-f0-9]{64})"$/);
  if (!match) return json({ error: "Approved revision ETag (If-Match) required" }, 400);
  const ifMatch = match[1];
  try {
    const body = await readObject(req, 64 * 1024);
    if (body.confirm_processing_consent !== true) {
      return json({ error: "Explicit AI processing consent confirmation required" }, 400);
    }
    const panelRes = await namedCall("collection-panel", { job_id: id }, session, 20000);
    if (!panelRes.ok) {
      return json({ error: "Collection review unavailable for this job or session." }, panelRes.status);
    }
    const panel = await panelRes.json();
    if (panel.job_id !== id) return json({ error: "Worker job mismatch" }, 502);
    if (panel.etag !== ifMatch) return json({ error: "Review changed. Refresh before executing AI." }, 412);
    if (panel.state !== "approved" || !panel.start_authorized) {
      return json({ error: "Independent approval and Start AI authorization required first." }, 409);
    }
    const eligibleIds: string[] = Array.isArray(panel.eligible_assets) ? panel.eligible_assets : [];
    const inheritedIds: string[] =
      panel.inherited_assets && typeof panel.inherited_assets === "object"
        ? Object.keys(panel.inherited_assets)
        : [];
    const needed = new Set<string>([...eligibleIds, ...inheritedIds]);
    if (needed.size === 0) {
      return json({ error: "No eligible assets in the approved revision to send to AI." }, 409);
    }
    const manifestRes = await fetch(workerUrl(`/jobs/${id}/manifest`), {
      headers: workerHeaders(),
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!manifestRes.ok) return json({ error: "Job manifest unavailable" }, manifestRes.status);
    const manifest = await manifestRes.json();
    const consent: Record<string, { source_url: string; sha256: string }> = {};
    for (const asset of manifest.assets || []) {
      if (
        asset &&
        typeof asset.asset_id === "string" &&
        needed.has(asset.asset_id) &&
        typeof asset.source_url === "string" &&
        asset.source_url.trim() &&
        typeof asset.sha256 === "string" &&
        asset.sha256.trim()
      ) {
        consent[asset.asset_id] = {
          source_url: asset.source_url,
          sha256: asset.sha256,
        };
      }
    }
    if (Object.keys(consent).length === 0) {
      return json({ error: "No downloadable assets with verified checksums found in manifest." }, 409);
    }
    const execRes = await namedCall(
      "collection-execute",
      { job_id: id, if_match: ifMatch, processing_consent: consent, async: true },
      session,
      15000,
    );
    const execData = await execRes.json().catch(() => ({}));
    if (!execRes.ok) {
      return json(
        { error: typeof execData.detail === "string" ? execData.detail : "AI execution rejected by worker." },
        execRes.status,
      );
    }
    return json(execData, execRes.status);
  } catch (error) {
    return json(
      { error: "AI execution request unavailable" },
      error instanceof BodyError ? error.status : 503,
    );
  }
}
