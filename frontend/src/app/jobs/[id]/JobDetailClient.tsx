"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import CollectionReview from "./CollectionReview";
import { api } from "@/lib/api";
import type {
  JobRecord,
  PipelineStatus,
  Manifest,
  ManifestAsset,
  Curation,
  CurationImageRec,
  CurationVideoRec,
  CurationRejected,
  Delivery,
  RightsReview,
} from "@/lib/types";
import { curationSelection, isMockCuration } from "@/lib/types";

const HAPPY_STAGES = [
  "queued",
  "collecting",
  "preprocessing",
  "ai_processing",
  "awaiting_rights_approval",
  "delivering",
  "completed",
] as const;

type GalleryFilter = "all" | "selected" | "images" | "videos" | "website" | "facebook";

function stagePresentation(stage: string | undefined): { width: number; barClass: string; badgeClass: string } {
  const s = String(stage || "");
  if (s === "completed") return { width: 100, barClass: "bg-green-600", badgeClass: "bg-green-100 text-green-800" };
  if (s === "partial_success") return { width: 85, barClass: "bg-amber-500", badgeClass: "bg-amber-100 text-amber-800" };
  if (s === "blocked") return { width: 50, barClass: "bg-amber-600", badgeClass: "bg-amber-100 text-amber-900" };
  if (s === "failed") return { width: 100, barClass: "bg-red-600", badgeClass: "bg-red-100 text-red-800" };
  const idx = HAPPY_STAGES.indexOf(s as (typeof HAPPY_STAGES)[number]);
  const width = idx >= 0 ? Math.round(((idx + 1) / HAPPY_STAGES.length) * 100) : 10;
  return { width, barClass: "bg-blue-600", badgeClass: "bg-blue-100 text-blue-800" };
}

function defaultBusinessName(job: JobRecord | null, delivery: Delivery | null): string {
  if (delivery?.folder_name) return delivery.folder_name;
  if (job?.website_url) {
    try {
      return new URL(job.website_url).hostname.replace(/^www\./, "");
    } catch {
      return "Pet Grooming Business";
    }
  }
  return "Pet Grooming Business";
}

function dateBadge(asset: ManifestAsset): { label: string; className: string } {
  if (asset.published_at_utc) {
    return {
      label: `Published ${asset.published_at_utc.slice(0, 10)}`,
      className: "bg-emerald-50 text-emerald-700 border-emerald-200",
    };
  }
  if (asset.platform === "website") {
    return {
      label: "Website · Date unverified",
      className: "bg-amber-50 text-amber-800 border-amber-200",
    };
  }
  return {
    label: "Date unknown",
    className: "bg-slate-100 text-slate-600 border-slate-200",
  };
}

export default function JobDetailClient({ id }: { id: string }) {
  const [job, setJob] = useState<JobRecord | null>(null);
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [curation, setCuration] = useState<Curation | null>(null);
  const [delivery, setDelivery] = useState<Delivery | null>(null);
  const [rights, setRights] = useState<RightsReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshTick, setRefreshTick] = useState(0);

  // Gallery filter & pagination
  const [filter, setFilter] = useState<GalleryFilter>("all");
  const [visibleCount, setVisibleCount] = useState(24);

  // Per-asset rights verification state
  const [evidenceRef, setEvidenceRef] = useState("");
  const [rightsReason, setRightsReason] = useState("");
  const [rightsBusy, setRightsBusy] = useState<string | null>(null);
  const [rightsMessage, setRightsMessage] = useState<string | null>(null);

  // Google Drive delivery trigger state
  const [businessName, setBusinessName] = useState("");
  const [releaseConfirmed, setReleaseConfirmed] = useState(false);
  const [delivering, setDelivering] = useState(false);
  const [deliveryMessage, setDeliveryMessage] = useState<string | null>(null);

  const triggerRefresh = useCallback(() => {
    setRefreshTick((t) => t + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function loadAll() {
      let shouldContinuePolling = false;
      try {
        const [j, s, m, c, d, r] = await Promise.all([
          api<JobRecord>(`/api/jobs/${id}`, options),
          api<PipelineStatus>(`/api/jobs/${id}/status`, options),
          api<Manifest>(`/api/jobs/${id}/manifest`, options).catch(() => null),
          api<Curation>(`/api/jobs/${id}/curation`, options).catch(() => null),
          api<Delivery>(`/api/jobs/${id}/delivery`, options).catch(() => null),
          api<RightsReview>(`/api/jobs/${id}/rights`, options).catch(() => null),
        ]);
        if (controller.signal.aborted) return;
        const aiActive = ["queued", "started"].includes(String(j?.ai_curation?.status || ""));
        shouldContinuePolling = !s.terminal || aiActive;
        setError(null);
        setJob(j);
        setStatus(s);
        setManifest(m);
        setCuration(c);
        setDelivery(d);
        setRights(r);
        setBusinessName((prev) => prev || defaultBusinessName(j, d));
      } catch (e: unknown) {
        if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (shouldContinuePolling) timer = setTimeout(loadAll, 5000);
        }
      }
    }
    void loadAll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id, refreshTick]);

  const mock = isMockCuration(curation);
  const selection = curationSelection(curation);

  const { selectedSet, imageRecById, videoRecById, rejectedById } = useMemo(() => {
    const selected = new Set<string>(selection?.selected_asset_ids || []);
    const imgMap = new Map<string, CurationImageRec>();
    const vidMap = new Map<string, CurationVideoRec>();
    const rejMap = new Map<string, CurationRejected>();
    for (const rec of selection?.best_images || []) {
      if (rec?.asset_id) imgMap.set(rec.asset_id, rec);
    }
    for (const rec of selection?.best_videos || []) {
      if (rec?.asset_id) vidMap.set(rec.asset_id, rec);
    }
    for (const rec of selection?.rejected_assets || []) {
      if (rec?.asset_id) rejMap.set(rec.asset_id, rec);
    }
    return { selectedSet: selected, imageRecById: imgMap, videoRecById: vidMap, rejectedById: rejMap };
  }, [selection]);

  const primaryAssets = useMemo(() => {
    return (manifest?.assets || []).filter((a) => a && a.type !== "frame");
  }, [manifest]);

  const filteredAssets = useMemo(() => {
    return primaryAssets.filter((a) => {
      if (filter === "selected") return selectedSet.has(a.asset_id);
      if (filter === "images") return a.type === "image" || String(a.mime || "").startsWith("image/");
      if (filter === "videos") return a.type === "video" || String(a.mime || "").startsWith("video/");
      if (filter === "website") return a.platform === "website";
      if (filter === "facebook") return a.platform === "facebook";
      return true;
    });
  }, [primaryAssets, filter, selectedSet]);

  async function recordAssetRights(assetId: string, decision: "verified" | "rejected" | "revoked") {
    if (!rights || rightsBusy) return;
    if (!evidenceRef.trim() || !rightsReason.trim()) {
      setRightsMessage("Enter both a rights evidence reference and a decision reason before recording.");
      return;
    }
    setRightsBusy(assetId);
    setRightsMessage(null);
    try {
      const res = await fetch(`/api/jobs/${id}/rights`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "If-Match": '"' + rights.etag + '"',
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          asset_id: assetId,
          decision,
          reason: rightsReason.trim(),
          evidence_ref: evidenceRef.trim(),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setRightsMessage(data.error || "Rights decision rejected.");
        return;
      }
      setRights(data as RightsReview);
      setRightsMessage(`Recorded "${decision}" for ${assetId}.`);
    } catch {
      setRightsMessage("Rights decision request failed.");
    } finally {
      setRightsBusy(null);
    }
  }

  async function triggerDriveDelivery() {
    if (delivering || !releaseConfirmed) return;
    setDelivering(true);
    setDeliveryMessage(null);
    try {
      const res = await fetch(`/api/jobs/${id}/delivery`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          authorization_confirmed: true,
          business_name: businessName.trim() || defaultBusinessName(job, delivery),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setDeliveryMessage(data.error || `Drive delivery failed (HTTP ${res.status}).`);
        return;
      }
      setReleaseConfirmed(false);
      setDeliveryMessage(
        data.status === "completed"
          ? "Google Drive delivery completed."
          : `Delivery response: ${data.status || "received"}${data.reason ? ` — ${data.reason}` : ""}`,
      );
      triggerRefresh();
    } catch {
      setDeliveryMessage("Google Drive delivery request unavailable.");
    } finally {
      setDelivering(false);
    }
  }

  if (loading) return <div className="p-4 text-sm text-slate-500">Loading…</div>;
  if (error) return <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>;
  if (!job) return <div className="p-4 text-sm text-slate-500">Job not found.</div>;

  const visual = stagePresentation(status?.stage);
  const verifiedSet = new Set(rights?.verified_asset_ids || []);
  const selectedIds = selection?.selected_asset_ids || [];
  const verifiedSelectedCount = selectedIds.filter((aid) => verifiedSet.has(aid)).length;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">Job {job.job_id}</h1>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={triggerRefresh}
            className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50"
          >
            Refresh
          </button>
          <Link href="/" className="text-sm text-blue-600 hover:underline">
            ← Dashboard
          </Link>
        </div>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm">
          <div className="flex items-center gap-2">
            <span className="font-medium">Pipeline stage:</span>
            <span className={`rounded px-2 py-0.5 font-mono text-xs font-medium ${visual.badgeClass}`}>
              {status?.stage ?? "—"}
            </span>
          </div>
          <span className="text-slate-500">{status?.detail}</span>
        </div>
        <div className="h-2 w-full rounded bg-slate-100">
          <div className={`h-2 rounded ${visual.barClass}`} style={{ width: `${visual.width}%` }} />
        </div>
        <div className="mt-2 text-xs text-slate-500">
          Stage progress indicator (happy-path pipeline stages; partial/blocked/failed shown with honest status badges).
        </div>
      </div>

      <CollectionReview
        key={id}
        id={id}
        includeUndatedWebsite={Boolean(job.include_undated_website)}
        aiStatus={job.ai_curation?.status ?? null}
        onChanged={triggerRefresh}
      />

      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-medium">Source outcomes</h2>
        <pre className="overflow-auto rounded bg-slate-50 p-3 text-xs">{JSON.stringify(job.sources, null, 2)}</pre>
        <div className="mt-3 grid grid-cols-1 gap-2 text-sm sm:grid-cols-3">
          <div>
            Preprocessing: <span className="font-mono">{job.preprocessing?.status ?? "—"}</span>
          </div>
          <div>
            AI curation: <span className="font-mono">{job.ai_curation?.status ?? "not_requested"}</span>
          </div>
          <div>
            Undated website media:{" "}
            <span className="font-mono">{job.include_undated_website ? "included" : "excluded"}</span>
          </div>
        </div>
        {job.error && (
          <div className="mt-3 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{job.error}</div>
        )}
      </section>

      <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-medium">Media gallery & AI curation ({primaryAssets.length} assets)</h2>
          <div className="flex flex-wrap gap-1.5 text-xs">
            {(
              [
                ["all", `All (${primaryAssets.length})`],
                ["selected", `AI Selected (${selectedSet.size})`],
                ["images", "Images"],
                ["videos", "Videos"],
                ["website", "Website"],
                ["facebook", "Facebook"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => {
                  setFilter(key);
                  setVisibleCount(24);
                }}
                className={`rounded border px-2.5 py-1 ${
                  filter === key
                    ? "border-blue-600 bg-blue-600 text-white"
                    : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {mock && (
          <div className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
            Mock/fixture curation detected — not shown as genuine AI recommendations.
          </div>
        )}
        {!selection && !mock && <div className="text-sm text-slate-500">No genuine AI curation recorded yet.</div>}
        {selection && (
          <div className="grid gap-2 rounded border border-slate-200 bg-slate-50 p-3 text-xs sm:grid-cols-4">
            <div>
              <span className="text-slate-500">Mode:</span> <span className="font-mono">{selection.mode ?? curation?.status}</span>
            </div>
            <div>
              <span className="text-slate-500">Selected:</span>{" "}
              <strong>{selectedIds.length}</strong> ({selection.best_images?.length ?? 0} img /{" "}
              {selection.best_videos?.length ?? 0} vid)
            </div>
            <div>
              <span className="text-slate-500">Rights verified:</span>{" "}
              <strong>
                {verifiedSelectedCount} / {selectedIds.length}
              </strong>
            </div>
            <div>
              <span className="text-slate-500">Prompt:</span>{" "}
              <span className="font-mono">
                {curation?.ai_envelope?.prompt_version || "v2"}
                {curation?.ai_envelope?.prompt_sha256 ? ` (${curation.ai_envelope.prompt_sha256.slice(0, 8)})` : ""}
              </span>
            </div>
          </div>
        )}

        {rights && primaryAssets.length > 0 && (
          <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-3 text-xs">
            <div className="font-medium text-slate-800">
              Per-Asset Commercial Rights Verification (M4 Immutable Ledger · Revision {rights.revision})
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <input
                type="text"
                maxLength={200}
                value={evidenceRef}
                onChange={(e) => setEvidenceRef(e.target.value)}
                placeholder="Evidence reference (e.g. client-email-2026-11-25)"
                className="rounded border border-slate-300 bg-white px-2.5 py-1.5"
              />
              <input
                type="text"
                maxLength={300}
                value={rightsReason}
                onChange={(e) => setRightsReason(e.target.value)}
                placeholder="Reason (e.g. Client confirmed ownership & release)"
                className="rounded border border-slate-300 bg-white px-2.5 py-1.5"
              />
            </div>
            {rightsMessage && (
              <p className="text-slate-700" role="status">
                {rightsMessage}
              </p>
            )}
          </div>
        )}

        {filteredAssets.length > 0 ? (
          <>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {filteredAssets.slice(0, visibleCount).map((a) => {
                const downloaded = a.extraction_status === "downloaded" && Boolean(a.local_path);
                const isVideo = a.type === "video" || String(a.mime || "").startsWith("video/");
                const fileHref = downloaded
                  ? `/api/jobs/${id}/file?path=${encodeURIComponent(a.local_path || "")}`
                  : "";
                const posterPath = a.frames?.[0]?.local_path;
                const posterHref = posterPath
                  ? `/api/jobs/${id}/file?path=${encodeURIComponent(posterPath)}`
                  : undefined;
                const imgRec = imageRecById.get(a.asset_id);
                const vidRec = videoRecById.get(a.asset_id);
                const rejRec = rejectedById.get(a.asset_id);
                const isSelected = selectedSet.has(a.asset_id);
                const badge = dateBadge(a);
                const ledgerRight = rights?.verified_rights?.[a.asset_id];

                return (
                  <div
                    key={a.asset_id}
                    className={`flex flex-col justify-between rounded border p-2.5 text-xs ${
                      isSelected ? "border-blue-400 bg-blue-50/30" : "border-slate-200 bg-white"
                    }`}
                  >
                    <div>
                      <div className="mb-2 aspect-square w-full overflow-hidden rounded bg-slate-100">
                        {downloaded && isVideo ? (
                          <video
                            controls
                            preload="metadata"
                            poster={posterHref}
                            src={fileHref}
                            className="h-full w-full object-cover"
                          />
                        ) : downloaded ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img
                            src={fileHref}
                            alt={a.asset_id}
                            className="h-full w-full object-cover"
                            loading="lazy"
                          />
                        ) : (
                          <div className="flex h-full w-full flex-col items-center justify-center p-2 text-center text-slate-500">
                            <span className="font-medium">No local binary</span>
                            <span className="font-mono text-[11px]">{a.extraction_status || "metadata_only"}</span>
                          </div>
                        )}
                      </div>

                      <div className="flex flex-wrap items-center gap-1">
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">
                          {a.platform || "media"}
                        </span>
                        <span className={`rounded border px-1.5 py-0.5 text-[11px] ${badge.className}`}>
                          {badge.label}
                        </span>
                      </div>

                      <div className="mt-1 truncate font-mono font-medium" title={a.asset_id}>
                        {a.asset_id}
                      </div>
                      <div className="text-slate-500">
                        {a.width && a.height ? `${a.width}×${a.height} · ` : ""}
                        {a.bytes ? `${Math.round(a.bytes / 1024)} KB` : "0 B"}
                      </div>

                      {imgRec && (
                        <div className="mt-1.5 rounded bg-blue-50 p-1.5 text-[11px] text-blue-900">
                          <div className="font-semibold">
                            AI Selected · {imgRec.recommended_section || "gallery"}
                            {imgRec.hero_rank ? ` · Hero #${imgRec.hero_rank}` : ""}
                            {typeof imgRec.overall_score === "number" ? ` · ${imgRec.overall_score}/100` : ""}
                          </div>
                          {imgRec.focal_point && <div>Focus: {imgRec.focal_point}</div>}
                          {imgRec.crop_guidance && <div>Crop: {imgRec.crop_guidance}</div>}
                        </div>
                      )}
                      {vidRec && (
                        <div className="mt-1.5 rounded bg-indigo-50 p-1.5 text-[11px] text-indigo-900">
                          <div className="font-semibold">
                            AI Video · {vidRec.recommended_section || "feature"}
                            {typeof vidRec.overall_score === "number" ? ` · ${vidRec.overall_score}/100` : ""}
                          </div>
                        </div>
                      )}
                      {rejRec && !isSelected && (
                        <div className="mt-1.5 rounded bg-slate-100 p-1.5 text-[11px] text-slate-700">
                          AI Rejected: {rejRec.reason || (rejRec.reasons || []).join(", ") || "lower score"}
                        </div>
                      )}
                    </div>

                    <div className="mt-2 border-t border-slate-100 pt-2">
                      <div className="mb-1 text-[11px]">
                        Rights:{" "}
                        <span
                          className={
                            ledgerRight || a.rights_status === "verified"
                              ? "font-semibold text-green-700"
                              : "text-amber-700"
                          }
                        >
                          {ledgerRight
                            ? `Verified (${ledgerRight.evidence_ref})`
                            : a.rights_status ?? "unverified"}
                        </span>
                      </div>
                      {rights && downloaded && (
                        <div className="flex flex-wrap gap-1">
                          <button
                            type="button"
                            disabled={rightsBusy === a.asset_id}
                            onClick={() => recordAssetRights(a.asset_id, "verified")}
                            className="rounded border border-green-300 bg-green-50 px-2 py-0.5 text-[11px] font-medium text-green-800 hover:bg-green-100 disabled:opacity-50"
                          >
                            Verify
                          </button>
                          <button
                            type="button"
                            disabled={rightsBusy === a.asset_id}
                            onClick={() => recordAssetRights(a.asset_id, "rejected")}
                            className="rounded border border-red-300 bg-red-50 px-2 py-0.5 text-[11px] font-medium text-red-800 hover:bg-red-100 disabled:opacity-50"
                          >
                            Reject
                          </button>
                          {ledgerRight && (
                            <button
                              type="button"
                              disabled={rightsBusy === a.asset_id}
                              onClick={() => recordAssetRights(a.asset_id, "revoked")}
                              className="rounded border border-amber-300 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800 hover:bg-amber-100 disabled:opacity-50"
                            >
                              Revoke
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
            {filteredAssets.length > visibleCount && (
              <div className="flex items-center gap-3 pt-2 text-sm">
                <button
                  type="button"
                  onClick={() => setVisibleCount((c) => c + 24)}
                  className="rounded border border-slate-300 px-3 py-1.5 hover:bg-slate-50"
                >
                  Load 24 more ({filteredAssets.length - visibleCount} remaining)
                </button>
                <button
                  type="button"
                  onClick={() => setVisibleCount(filteredAssets.length)}
                  className="text-blue-600 hover:underline"
                >
                  Show all ({filteredAssets.length})
                </button>
              </div>
            )}
          </>
        ) : (
          <div className="text-sm text-slate-500">No assets match the current filter.</div>
        )}
      </section>

      <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="font-medium">Google Drive Delivery</h2>
        {!delivery && <div className="text-sm text-slate-500">No delivery recorded.</div>}
        {delivery && (
          <div className="space-y-2 text-sm">
            <div>
              Status: <span className="font-mono">{delivery.status}</span>
            </div>
            {delivery.drive_folder_id && (
              <div>
                Drive folder: <span className="font-mono">{delivery.drive_folder_id}</span>
              </div>
            )}
            {delivery.drive_folder_link && (
              <a
                className="inline-block font-medium text-blue-600 hover:underline"
                href={delivery.drive_folder_link}
                target="_blank"
                rel="noreferrer"
              >
                Open Google Drive folder →
              </a>
            )}
            {delivery.uploaded && delivery.uploaded.length > 0 && (
              <ul className="mt-2 space-y-1">
                {delivery.uploaded.map((u) => (
                  <li key={u.asset_id} className="font-mono text-xs">
                    {u.filename || u.asset_id}: <strong>{u.status}</strong>{" "}
                    {u.drive_link ? (
                      <a href={u.drive_link} target="_blank" rel="noreferrer" className="text-blue-600 underline">
                        ({u.drive_file_id})
                      </a>
                    ) : u.drive_file_id ? (
                      `(${u.drive_file_id})`
                    ) : (
                      ""
                    )}{" "}
                    {u.error ? `— ${u.error}` : ""}
                  </li>
                ))}
              </ul>
            )}
            {delivery.pending_rights_ids && delivery.pending_rights_ids.length > 0 && (
              <div className="text-amber-700">Pending rights approval: {delivery.pending_rights_ids.join(", ")}</div>
            )}
            {delivery.errors && delivery.errors.length > 0 && (
              <div className="text-red-700">Errors: {delivery.errors.join("; ")}</div>
            )}
          </div>
        )}

        {selection && !mock && (
          <div className="space-y-3 rounded border border-slate-200 bg-slate-50 p-3 text-xs">
            <div className="font-medium text-slate-800">
              Deliver Verified Media to Private Google Drive ({verifiedSelectedCount}/{selectedIds.length} AI-selected
              assets rights-verified)
            </div>
            <label className="block max-w-sm">
              Business / Folder name
              <input
                type="text"
                maxLength={120}
                value={businessName}
                onChange={(e) => setBusinessName(e.target.value)}
                className="mt-1 block w-full rounded border border-slate-300 bg-white px-2.5 py-1.5"
              />
            </label>
            <label className="flex items-start gap-2 text-slate-800">
              <input
                type="checkbox"
                checked={releaseConfirmed}
                onChange={(e) => setReleaseConfirmed(e.target.checked)}
                className="mt-0.5"
              />
              <span>
                I confirm commercial release rights are verified for the approved assets and authorize private upload to
                Google Drive (no public sharing).
              </span>
            </label>
            {deliveryMessage && (
              <p className="text-slate-700" role="status">
                {deliveryMessage}
              </p>
            )}
            <button
              type="button"
              disabled={delivering || !releaseConfirmed}
              onClick={triggerDriveDelivery}
              className="rounded bg-green-600 px-3 py-1.5 font-medium text-white hover:bg-green-700 disabled:opacity-50"
            >
              {delivering ? "Uploading via n8n…" : "Deliver Approved Media to Google Drive"}
            </button>
          </div>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-medium">Manifest metadata</h2>
        <pre className="overflow-auto rounded bg-slate-50 p-3 text-xs">
          {JSON.stringify(
            { counts: manifest?.counts ?? null, coverage: manifest?.coverage ?? null },
            null,
            2,
          )}
        </pre>
      </section>
    </div>
  );
}
