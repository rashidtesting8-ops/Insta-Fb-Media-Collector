"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import {
  ArrowRightIcon,
  CheckIcon,
  ExpandIcon,
  FolderCloudIcon,
  GlobeIcon,
  RefreshIcon,
  ShieldCheckIcon,
  SparklesIcon,
} from "@/components/Icons";

const HAPPY_STAGES = [
  "queued",
  "collecting",
  "preprocessing",
  "ai_processing",
  "awaiting_rights_approval",
  "delivering",
  "completed",
] as const;

const JOURNEY_STEPS = [
  { num: 1, title: "Collect Media", sub: "Website & Facebook" },
  { num: 2, title: "Admin Review", sub: "Approve revision" },
  { num: 3, title: "AI Selection", sub: "Visual Director V2" },
  { num: 4, title: "Rights Check", sub: "Verify permissions" },
  { num: 5, title: "Drive Delivery", sub: "Private folder" },
] as const;

type GalleryFilter = "all" | "downloaded" | "selected" | "images" | "videos" | "website" | "facebook";

function stagePresentation(stage: string | undefined): {
  label: string;
  width: number;
  barClass: string;
  badgeClass: string;
  activeStep: number;
} {
  const s = String(stage || "");
  if (s === "completed")
    return {
      label: "Completed & Delivered",
      width: 100,
      barClass: "bg-emerald-600",
      badgeClass: "border-emerald-200 bg-emerald-50 text-emerald-800",
      activeStep: 6,
    };
  if (s === "delivering")
    return {
      label: "Uploading to Google Drive…",
      width: 90,
      barClass: "bg-teal-600",
      badgeClass: "border-teal-200 bg-teal-50 text-teal-800",
      activeStep: 5,
    };
  if (s === "awaiting_rights_approval")
    return {
      label: "Ready for Rights Check",
      width: 78,
      barClass: "bg-teal-600",
      badgeClass: "border-teal-200 bg-teal-50 text-teal-900",
      activeStep: 4,
    };
  if (s === "ai_processing")
    return {
      label: "AI Curating Media…",
      width: 58,
      barClass: "bg-teal-600",
      badgeClass: "border-sky-200 bg-sky-50 text-sky-900",
      activeStep: 3,
    };
  if (s === "partial_success")
    return {
      label: "Partial Collection · Review Needed",
      width: 42,
      barClass: "bg-amber-500",
      badgeClass: "border-amber-200 bg-amber-50 text-amber-900",
      activeStep: 2,
    };
  if (s === "blocked")
    return {
      label: "Paused / Awaiting Action",
      width: 45,
      barClass: "bg-amber-600",
      badgeClass: "border-amber-200 bg-amber-50 text-amber-950",
      activeStep: 2,
    };
  if (s === "failed")
    return {
      label: "Attention Needed",
      width: 100,
      barClass: "bg-rose-600",
      badgeClass: "border-rose-200 bg-rose-50 text-rose-800",
      activeStep: 2,
    };
  const idx = HAPPY_STAGES.indexOf(s as (typeof HAPPY_STAGES)[number]);
  const width = idx >= 0 ? Math.round(((idx + 1) / HAPPY_STAGES.length) * 100) : 14;
  return {
    label: s ? s.replace(/_/g, " ") : "Preparing…",
    width,
    barClass: "bg-[#0F766E]",
    badgeClass: "border-sky-200 bg-sky-50 text-sky-900",
    activeStep: 1,
  };
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

function friendlyFileName(asset: ManifestAsset): string {
  if (asset.local_path) {
    const parts = asset.local_path.split("/");
    const tail = parts[parts.length - 1];
    if (tail && tail.length <= 42) return tail;
  }
  return asset.asset_id.length > 22 ? `${asset.asset_id.slice(0, 12)}…${asset.asset_id.slice(-6)}` : asset.asset_id;
}

function dateBadge(asset: ManifestAsset): { label: string; className: string } {
  const verifiedDate =
    asset.published_at_utc || (asset.date_status === "verified" ? asset.post_date : null);
  if (verifiedDate) {
    return {
      label: `Published ${verifiedDate.slice(0, 10)}`,
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
    className: "bg-stone-100 text-stone-600 border-stone-200",
  };
}

const RIGHTS_PRESETS = [
  { ref: "client-email-release", reason: "Client confirmed ownership and commercial release by email" },
  { ref: "signed-onboarding-form", reason: "Authorized under signed client onboarding media agreement" },
] as const;

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
  const [reviewRefreshTick, setReviewRefreshTick] = useState(0);
  const lastReviewSyncKey = useRef<string | null>(null);
  const [copiedId, setCopiedId] = useState(false);

  // Gallery filter, pagination & photo lightbox
  const [filter, setFilter] = useState<GalleryFilter>("all");
  const [visibleCount, setVisibleCount] = useState(24);
  const [lightboxAsset, setLightboxAsset] = useState<{ asset: ManifestAsset; href: string } | null>(null);

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
  const [resuming, setResuming] = useState(false);
  const [resumeMessage, setResumeMessage] = useState<string | null>(null);

  // Owner-supplied Facebook ZIP / media upload state (Milestone OS-2)
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadDate, setUploadDate] = useState("");
  const [uploadingMedia, setUploadingMedia] = useState(false);
  const [uploadMessage, setUploadMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [copiedClientGuide, setCopiedClientGuide] = useState(false);

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
        const syncKey = `${s?.stage || ""}:${r?.revision ?? ""}:${j?.ai_curation?.status || ""}`;
        if (lastReviewSyncKey.current !== null && lastReviewSyncKey.current !== syncKey) {
          setReviewRefreshTick((t) => t + 1);
        }
        lastReviewSyncKey.current = syncKey;
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
    for (const rec of selection?.hero_selection || []) {
      if (rec?.asset_id && !imgMap.has(rec.asset_id)) {
        imgMap.set(rec.asset_id, {
          asset_id: rec.asset_id,
          recommended_section: "hero",
          hero_rank: rec.rank,
          focal_point: rec.focal_point,
          crop_guidance: [rec.desktop_crop, rec.mobile_crop].filter(Boolean).join(" / "),
          reason: rec.reason,
        });
      }
    }
    const sectionBuckets = [
      ["salon_facilities", selection?.salon_images],
      ["founder_team", selection?.founder_and_team_images],
      ["service", selection?.service_images],
      ["grooming_transformation", selection?.grooming_transformations],
      ["gallery", selection?.gallery_images],
    ] as const;
    for (const [secName, list] of sectionBuckets) {
      for (const rec of list || []) {
        if (rec?.asset_id && !imgMap.has(rec.asset_id)) {
          imgMap.set(rec.asset_id, {
            asset_id: rec.asset_id,
            recommended_section: rec.recommended_section || secName,
            focal_point: rec.focal_point ?? undefined,
            crop_guidance: rec.crop_notes ?? undefined,
            reason: rec.reason,
          });
        }
      }
    }
    for (const rec of selection?.best_videos || []) {
      if (rec?.asset_id) vidMap.set(rec.asset_id, rec);
    }
    for (const rec of selection?.video_recommendations || []) {
      if (rec?.asset_id && !vidMap.has(rec.asset_id)) {
        vidMap.set(rec.asset_id, {
          ...rec,
          overall_score: rec.overall_score ?? rec.quality_score,
        });
      }
    }
    for (const rec of selection?.rejected_assets || []) {
      if (rec?.asset_id) rejMap.set(rec.asset_id, rec);
    }
    return { selectedSet: selected, imageRecById: imgMap, videoRecById: vidMap, rejectedById: rejMap };
  }, [selection]);

  const primaryAssets = useMemo(() => {
    const list = (manifest?.assets || []).filter((a) => a && a.type !== "frame");
    // Surface downloaded local media before pending placeholders so operators inspect real photos first.
    return [...list].sort((a, b) => {
      const aReady = a.extraction_status === "downloaded" ? 0 : 1;
      const bReady = b.extraction_status === "downloaded" ? 0 : 1;
      return aReady - bReady;
    });
  }, [manifest]);

  const filteredAssets = useMemo(() => {
    return primaryAssets.filter((a) => {
      if (filter === "downloaded") return a.extraction_status === "downloaded";
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

  async function triggerResumeCollection() {
    if (resuming) return;
    setResuming(true);
    setResumeMessage(null);
    try {
      const res = await fetch(`/api/jobs/${id}/resume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setResumeMessage(data.error || `Unable to resume collection (HTTP ${res.status}).`);
        return;
      }
      setResumeMessage("Historical collection resumed from saved checkpoint — polling live progress.");
      triggerRefresh();
      setReviewRefreshTick((t) => t + 1);
    } catch {
      setResumeMessage("Resume collection request unavailable.");
    } finally {
      setResuming(false);
    }
  }

  async function handleFacebookMediaUpload(ev: React.FormEvent) {
    ev.preventDefault();
    if (!uploadFile || uploadingMedia) return;
    setUploadingMedia(true);
    setUploadMessage(null);
    try {
      const fd = new FormData();
      fd.append("file", uploadFile);
      if (uploadDate.trim()) fd.append("published_date", uploadDate.trim());
      const res = await fetch(`/api/jobs/${id}/media-upload`, {
        method: "POST",
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setUploadMessage({
          ok: false,
          text: typeof data.error === "string" ? data.error : `Upload rejected (HTTP ${res.status}).`,
        });
        return;
      }
      setUploadFile(null);
      const added = Number(data.uploaded_count || 0);
      const inWin = Number(data.in_window_count || 0);
      const dupes = Number(data.duplicate_count || 0);
      const rev = data.revision ? ` (Revision ${data.revision} ready for Admin review)` : "";
      setUploadMessage({
        ok: true,
        text:
          added > 0
            ? `Added ${added} Facebook file${added === 1 ? "" : "s"} (${inWin} in 365-day window)${dupes ? `, ${dupes} duplicate skipped` : ""}${rev}.`
            : `No new files added (${dupes} duplicate${dupes === 1 ? "" : "s"} already in project).`,
      });
      triggerRefresh();
      setReviewRefreshTick((t) => t + 1);
    } catch {
      setUploadMessage({ ok: false, text: "Client media upload service unavailable." });
    } finally {
      setUploadingMedia(false);
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

  function copyJobId() {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(id);
      setCopiedId(true);
      setTimeout(() => setCopiedId(false), 1800);
    }
  }

  function copyClientExportMessage() {
    const name = defaultBusinessName(job, delivery);
    const msg =
      `Hi! To include your best Facebook photos & videos from the past 12 months for ${name} ` +
      `without needing your login password, please export a quick Facebook Page ZIP:\n` +
      `1. Open Facebook → Settings & Privacy → Settings → Accounts Center → Your information and permissions\n` +
      `2. Click "Download your information" → select your Business Page → choose "Posts" (Photos & Videos)\n` +
      `3. Set Date range = "Last year" and Format = "JSON"\n` +
      `4. Click Create file and send us the .zip file (or send your favorite grooming photos/videos directly).`;
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(msg);
      setCopiedClientGuide(true);
      setTimeout(() => setCopiedClientGuide(false), 2200);
    }
  }

  if (loading) {
    return (
      <div className="space-y-5">
        <div className="h-9 w-64 animate-pulse rounded-xl bg-[#EAE2D8]" />
        <div className="studio-card h-32 animate-pulse bg-white/70" />
        <div className="studio-card h-56 animate-pulse bg-white/70" />
      </div>
    );
  }
  if (error) {
    return (
      <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">
        {error}
      </div>
    );
  }
  if (!job) return <div className="p-4 text-sm text-[#6B635B]">Job not found.</div>;

  const visual = stagePresentation(status?.stage);
  const salonName = defaultBusinessName(job, delivery);
  const verifiedSet = new Set(rights?.verified_asset_ids || []);
  const selectedIds = selection?.selected_asset_ids || [];
  const verifiedSelectedCount = selectedIds.filter((aid) => verifiedSet.has(aid)).length;
  const downloadedCount = primaryAssets.filter((a) => a.extraction_status === "downloaded").length;

  let nextStepTitle = "Review collected media";
  let nextStepHint = "Check the gathered photos & videos below, then have a second Named Admin approve and start AI curation.";
  if (status?.stage === "completed") {
    nextStepTitle = "Project complete & delivered";
    nextStepHint = "Approved media and sanitized AI reports are safely stored in your private Google Drive folder.";
  } else if (status?.stage === "awaiting_rights_approval" || (selection && !mock && delivery?.status !== "completed")) {
    nextStepTitle =
      verifiedSelectedCount > 0
        ? "Ready to deliver to Google Drive"
        : "Verify commercial rights on AI-selected media";
    nextStepHint =
      verifiedSelectedCount > 0
        ? `${verifiedSelectedCount} of ${selectedIds.length} AI-selected asset(s) are rights-verified. Scroll to Google Drive Delivery to upload.`
        : "Fill in the rights evidence reference below and click Verify on each approved asset you want to include in Google Drive.";
  } else if (["queued", "collecting", "preprocessing", "ai_processing", "delivering"].includes(String(status?.stage || ""))) {
    nextStepTitle = "Working automatically in background";
    nextStepHint = "This page refreshes every 5 seconds — no action needed while processing finishes.";
  }

  return (
    <div className="space-y-6">
      {/* Studio Project Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Link href="/" className="font-medium text-[#0F766E] hover:underline">
              ← Dashboard
            </Link>
            <span className="text-[#C7BCAE]">/</span>
            <button
              type="button"
              onClick={copyJobId}
              className="rounded-md bg-[#F2ECE4] px-2 py-0.5 font-mono text-[11px] text-[#6B635B] transition hover:bg-[#E7DFD5]"
              title="Click to copy full Job ID"
            >
              {copiedId ? "Copied Job ID ✓" : `Job ${job.job_id}`}
            </button>
          </div>
          <h1 className="truncate font-serif text-3xl font-medium tracking-tight text-[#181614]">{salonName}</h1>
          <div className="flex flex-wrap items-center gap-3 text-xs text-[#6B635B]">
            {job.website_url && (
              <a
                href={job.website_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 hover:text-[#0F766E] hover:underline"
              >
                <GlobeIcon className="h-3.5 w-3.5" />
                {job.website_url}
              </a>
            )}
            <span>· {downloadedCount} downloaded file{downloadedCount === 1 ? "" : "s"}</span>
            {selection && !mock && <span>· {selectedIds.length} AI selected</span>}
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <button type="button" onClick={triggerRefresh} className="studio-btn-secondary">
            <RefreshIcon className="h-4 w-4" />
            Refresh
          </button>
          {delivery?.drive_folder_link && (
            <a
              href={delivery.drive_folder_link}
              target="_blank"
              rel="noreferrer"
              className="studio-btn-primary"
            >
              <FolderCloudIcon className="h-4 w-4" />
              Open Google Drive
            </a>
          )}
        </div>
      </div>

      {/* 5-Step Journey Card + Dynamic Next-Step Guidance */}
      <section className="studio-card space-y-5 p-5 sm:p-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold uppercase tracking-wider text-[#7A7168]">Project Progress</span>
            <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold ${visual.badgeClass}`}>
              {visual.label}
              <span className="font-mono text-[10px] opacity-75">({status?.stage ?? "—"})</span>
            </span>
          </div>
          {status?.detail && <span className="text-xs text-[#6B635B]">{status.detail}</span>}
        </div>

        {/* Visual 5-Step Journey */}
        <ol className="grid grid-cols-2 gap-2.5 sm:grid-cols-5">
          {JOURNEY_STEPS.map((step) => {
            const done = visual.activeStep > step.num;
            const current = visual.activeStep === step.num;
            return (
              <li
                key={step.num}
                className={`rounded-xl border px-3 py-2.5 transition ${
                  done
                    ? "border-emerald-200 bg-emerald-50/70"
                    : current
                      ? "border-[#0F766E] bg-teal-50/70 ring-2 ring-[#0F766E]/15"
                      : "border-[#EDE6DC] bg-[#FAF8F5]"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold ${
                      done
                        ? "bg-emerald-600 text-white"
                        : current
                          ? "bg-[#0F766E] text-white"
                          : "bg-[#E7DFD5] text-[#6B635B]"
                    }`}
                  >
                    {done ? <CheckIcon className="h-3 w-3" /> : step.num}
                  </span>
                  <span className="truncate text-xs font-semibold text-[#181614]">{step.title}</span>
                </div>
                <div className="mt-1 pl-7 text-[11px] text-[#7A7168]">{step.sub}</div>
              </li>
            );
          })}
        </ol>

        <div className="h-1.5 w-full overflow-hidden rounded-full bg-[#EFEAE2]">
          <div className={`h-full rounded-full transition-all duration-500 ${visual.barClass}`} style={{ width: `${visual.width}%` }} />
        </div>

        {/* Plain-English Next Action Banner */}
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[#E6DDD2] bg-[#FAF8F5] px-4 py-3 text-xs">
          <div>
            <span className="font-semibold text-[#181614]">Next step: {nextStepTitle}.</span>{" "}
            <span className="text-[#5C534B]">{nextStepHint}</span>
          </div>
          {job.collection_mode === "public_checkpoint" &&
            !["queued", "running"].includes(String(job.status)) &&
            !["queued", "started"].includes(String(job.ai_curation?.status || "")) && (
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={resuming}
                onClick={triggerResumeCollection}
                className="studio-btn-secondary py-1.5 text-xs"
              >
                {resuming ? "Resuming checkpoint…" : "Resume Collection (Next Batch)"}
              </button>
            </div>
          )}
        </div>
        {resumeMessage && (
          <p className="text-xs font-medium text-[#0F766E]" role="status">
            {resumeMessage}
          </p>
        )}
        {job.error && (
          <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800">{job.error}</div>
        )}
      </section>

      {/* Independent Admin Review & AI Launch */}
      <CollectionReview
        key={id}
        id={id}
        includeUndatedWebsite={Boolean(job.include_undated_website)}
        aiStatus={job.ai_curation?.status ?? null}
        externalRefreshTick={reviewRefreshTick}
        onChanged={triggerRefresh}
      />

      {/* Media Gallery & AI Curation */}
      <section className="studio-card space-y-5 p-5 sm:p-6">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-lg font-semibold text-[#181614]">
              Media gallery &amp; AI curation ({primaryAssets.length} assets)
            </h2>
            <p className="text-xs text-[#7A7168]">
              Inspect photos &amp; videos, review AI Visual Director notes, and verify commercial release rights.
            </p>
          </div>
          <div className="flex flex-wrap gap-1.5 text-xs">
            {(
              [
                ["all", `All (${primaryAssets.length})`],
                ["downloaded", `Downloaded (${downloadedCount})`],
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
                className={`rounded-full px-3 py-1.5 font-medium transition ${
                  filter === key
                    ? "bg-[#181614] text-white shadow-sm"
                    : "bg-[#FAF8F5] text-[#5C534B] ring-1 ring-[#E4DCD1] hover:bg-[#F2ECE4]"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {mock && (
          <div className="rounded-xl border border-amber-200 bg-amber-50 p-3.5 text-sm text-amber-900">
            Mock/fixture curation detected — not shown as genuine AI recommendations.
          </div>
        )}
        {!selection && !mock && (
          <div className="studio-card-soft px-4 py-3 text-sm text-[#6B635B]">
            No genuine AI curation recorded yet. Approve the collection above and launch AI Visual Director V2.
          </div>
        )}

        {/* Owner-Supplied Facebook Media / Export ZIP Upload (100% open-source, zero Meta API) */}
        {!selection && !["queued", "running"].includes(String(job.status)) && (
          <form
            onSubmit={handleFacebookMediaUpload}
            className="space-y-3 rounded-2xl border border-teal-200/80 bg-gradient-to-br from-teal-50/50 via-white to-[#FAF8F5] p-4 text-xs"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2 font-semibold text-[#181614]">
                <FolderCloudIcon className="h-4 w-4 text-[#0F766E]" />
                Add Client Facebook Media (Export ZIP or Photos / Videos)
              </div>
              <span className="rounded-full bg-white px-2.5 py-0.5 text-[11px] font-medium text-[#544B43] ring-1 ring-[#E4DCD1]">
                Up to 100 MB · SHA-256 &amp; FFmpeg verified
              </span>
            </div>
            <p className="leading-relaxed text-[#5C534B]">
              Facebook&apos;s CDN blocks automated bot downloads via <code>robots.txt</code>. Drop the client&apos;s official
              Facebook &ldquo;Download Page Information&rdquo; <code>.zip</code> (post dates read automatically) or any
              authorized Facebook photo/video file below to include it in AI curation and Google Drive delivery.
            </p>
            <div className="grid gap-3 sm:grid-cols-12">
              <label className="block sm:col-span-7">
                <span className="mb-1 block font-medium text-[#2C2824]">Facebook ZIP archive or photo / video</span>
                <input
                  type="file"
                  accept=".zip,image/jpeg,image/png,image/webp,image/gif,image/avif,video/mp4,video/quicktime,video/webm"
                  onChange={(e) => setUploadFile(e.target.files?.[0] || null)}
                  className="block w-full cursor-pointer rounded-xl border border-dashed border-[#C7BCAE] bg-white px-3 py-2 text-xs text-[#2C2824] file:mr-3 file:rounded-lg file:border-0 file:bg-[#F2ECE4] file:px-3 file:py-1 file:text-xs file:font-semibold file:text-[#181614] hover:border-[#0F766E]"
                />
              </label>
              <label className="block sm:col-span-5">
                <span className="mb-1 block font-medium text-[#2C2824]">Published date (optional for loose files)</span>
                <input
                  type="date"
                  value={uploadDate}
                  onChange={(e) => setUploadDate(e.target.value)}
                  className="studio-input py-2 text-xs"
                />
              </label>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-[11px] text-[#7A7168]">
                Uploaded files keep rights unverified and publish a new M4 revision for independent Admin review.
              </span>
              <button
                type="submit"
                disabled={uploadingMedia || !uploadFile}
                className="studio-btn-primary py-2 text-xs"
              >
                {uploadingMedia ? "Processing & Extracting Frames…" : "Upload Facebook Media"}
              </button>
            </div>
            {uploadMessage && (
              <p
                role="status"
                className={`font-medium ${uploadMessage.ok ? "text-emerald-800" : "text-rose-700"}`}
              >
                {uploadMessage.text}
              </p>
            )}
            <details className="rounded-xl border border-[#E5DDD2] bg-white/90 px-3.5 py-2.5 text-[#544B43]">
              <summary className="cursor-pointer font-semibold text-[#181614]">
                Need the client&apos;s Facebook photos? 60-second export guide &amp; copyable client message
              </summary>
              <div className="mt-2.5 space-y-2 text-[11px] leading-relaxed">
                <ol className="list-decimal space-y-1 pl-4">
                  <li>
                    In Facebook, open <strong>Settings &amp; Privacy → Settings → Accounts Center → Your information and permissions</strong>.
                  </li>
                  <li>
                    Choose <strong>Download your information</strong>, pick the salon&apos;s <strong>Facebook Page</strong>, and select <strong>Posts</strong> (photos &amp; videos).
                  </li>
                  <li>
                    Select <strong>Date range: Last year</strong> and <strong>Format: JSON</strong> (so original post dates are read automatically).
                  </li>
                  <li>
                    Download the <code>.zip</code> archive and upload it above (up to 60 photos &amp; videos in one upload).
                  </li>
                </ol>
                <button
                  type="button"
                  onClick={copyClientExportMessage}
                  className="studio-btn-secondary py-1.5 text-[11px]"
                >
                  {copiedClientGuide ? "Copied client message!" : "Copy WhatsApp / Email instructions for client"}
                </button>
              </div>
            </details>
          </form>
        )}

        {/* Editorial AI Direction Brief */}
        {selection && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div className="studio-card-soft p-3 text-xs">
                <div className="text-[#877D73]">AI Mode</div>
                <div className="mt-1 font-mono font-semibold text-[#181614]">{selection.mode ?? curation?.status}</div>
              </div>
              <div className="studio-card-soft p-3 text-xs">
                <div className="text-[#877D73]">AI Selected</div>
                <div className="mt-1 font-semibold text-[#0F766E]">
                  {selectedIds.length} <span className="font-normal text-[#6B635B]">({imageRecById.size} img / {videoRecById.size} vid)</span>
                </div>
              </div>
              <div className="studio-card-soft p-3 text-xs">
                <div className="text-[#877D73]">Rights Verified</div>
                <div className="mt-1 font-semibold text-[#181614]">
                  {verifiedSelectedCount} / {selectedIds.length}
                </div>
              </div>
              <div className="studio-card-soft p-3 text-xs">
                <div className="text-[#877D73]">Prompt Contract</div>
                <div className="mt-1 font-mono text-[#544B43]">
                  {curation?.ai_envelope?.prompt_version || "v2"}
                  {curation?.ai_envelope?.prompt_sha256 ? ` (${curation.ai_envelope.prompt_sha256.slice(0, 8)})` : ""}
                </div>
              </div>
            </div>

            {selection.business_visual_identity?.style_summary && (
              <div className="rounded-2xl border border-teal-200/80 bg-gradient-to-br from-teal-50/70 via-white to-amber-50/40 p-4 text-sm">
                <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-[#0F766E]">
                  <SparklesIcon className="h-4 w-4" />
                  AI Visual Direction Summary
                </div>
                <p className="mt-2 font-medium leading-relaxed text-[#181614]">
                  {selection.business_visual_identity.style_summary}
                </p>
                {selection.business_visual_identity.recommended_design_direction && (
                  <p className="mt-1.5 text-xs leading-relaxed text-[#5C534B]">
                    <strong className="font-semibold text-[#2C2824]">Design direction:</strong>{" "}
                    {selection.business_visual_identity.recommended_design_direction}
                  </p>
                )}
              </div>
            )}
          </div>
        )}

        {/* Per-Asset Rights Verification Toolbar */}
        {rights && primaryAssets.length > 0 && (
          <div className="space-y-3 rounded-2xl border border-[#E5DDD2] bg-[#FAF8F5] p-4 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2 font-semibold text-[#181614]">
                <ShieldCheckIcon className="h-4 w-4 text-[#0F766E]" />
                Per-Asset Commercial Rights Verification (M4 Immutable Ledger · Revision {rights.revision})
              </div>
              <div className="flex flex-wrap gap-1.5">
                {RIGHTS_PRESETS.map((p) => (
                  <button
                    key={p.ref}
                    type="button"
                    onClick={() => {
                      setEvidenceRef(p.ref);
                      setRightsReason(p.reason);
                    }}
                    className="rounded-full border border-[#DED5C8] bg-white px-2.5 py-0.5 text-[11px] font-medium text-[#544B43] transition hover:border-[#0F766E] hover:text-[#0F766E]"
                  >
                    + Fill &ldquo;{p.ref}&rdquo;
                  </button>
                ))}
              </div>
            </div>
            <p className="text-[#6B635B]">
              Step 1: Enter (or pick) your client release proof below. Step 2: Click the Verify button on each asset
              card you want to approve for Google Drive.
            </p>
            <div className="grid gap-2.5 sm:grid-cols-2">
              <input
                type="text"
                maxLength={200}
                value={evidenceRef}
                onChange={(e) => setEvidenceRef(e.target.value)}
                placeholder="Evidence reference (e.g. client-email-2026-11-25)"
                className="studio-input py-2 text-xs"
              />
              <input
                type="text"
                maxLength={300}
                value={rightsReason}
                onChange={(e) => setRightsReason(e.target.value)}
                placeholder="Reason (e.g. Client confirmed ownership & release)"
                className="studio-input py-2 text-xs"
              />
            </div>
            {rightsMessage && (
              <p className="font-medium text-[#0F766E]" role="status">
                {rightsMessage}
              </p>
            )}
          </div>
        )}

        {/* Gallery Grid */}
        {filteredAssets.length > 0 ? (
          <>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
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
                const isVerified = Boolean(ledgerRight || a.rights_status === "verified");

                return (
                  <article
                    key={a.asset_id}
                    className={`group flex flex-col justify-between overflow-hidden rounded-2xl border transition ${
                      isSelected
                        ? "border-[#0F766E] bg-teal-50/20 shadow-sm ring-1 ring-[#0F766E]/20"
                        : "border-[#E7DFD5] bg-white hover:border-[#CFC3B4]"
                    }`}
                  >
                    <div>
                      <div className="relative aspect-[4/3] w-full overflow-hidden bg-[#F2ECE4]">
                        {downloaded && isVideo ? (
                          <video
                            controls
                            preload="metadata"
                            poster={posterHref}
                            src={fileHref}
                            className="h-full w-full object-cover"
                          />
                        ) : downloaded ? (
                          <>
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img
                              src={fileHref}
                              alt={a.asset_id}
                              className="h-full w-full cursor-zoom-in object-cover transition duration-300 group-hover:scale-[1.02]"
                              loading="lazy"
                              onClick={() => setLightboxAsset({ asset: a, href: fileHref })}
                            />
                            <button
                              type="button"
                              onClick={() => setLightboxAsset({ asset: a, href: fileHref })}
                              aria-label={`Preview ${a.asset_id}`}
                              className="absolute right-2.5 top-2.5 flex h-7 w-7 items-center justify-center rounded-lg bg-[#181614]/70 text-white opacity-0 backdrop-blur-sm transition group-hover:opacity-100"
                            >
                              <ExpandIcon className="h-3.5 w-3.5" />
                            </button>
                          </>
                        ) : (
                          <div className="flex h-full w-full flex-col items-center justify-center p-4 text-center text-[#7A7168]">
                            <span className="text-xs font-semibold text-[#544B43]">No local binary</span>
                            <span className="mt-1 rounded-full bg-white px-2.5 py-0.5 font-mono text-[10px] ring-1 ring-[#E4DCD1]">
                              {a.failure_reason === "robots_disallowed"
                                ? "robots.txt blocked"
                                : a.extraction_status || "metadata_only"}
                            </span>
                            {a.failure_reason === "robots_disallowed" && (
                              <span className="mt-1.5 text-[11px] leading-snug text-[#7A7168]">
                                Blocked by platform robots.txt policy · Use Client Facebook Upload above
                              </span>
                            )}
                            {a.failure_reason && a.failure_reason !== "robots_disallowed" && (
                              <span className="mt-1.5 text-[11px] leading-snug text-[#7A7168]">
                                {a.failure_reason.replace(/_/g, " ")}
                              </span>
                            )}
                          </div>
                        )}
                        {isSelected && (
                          <span className="pointer-events-none absolute left-2.5 top-2.5 inline-flex items-center gap-1 rounded-full bg-[#0F766E] px-2.5 py-0.5 text-[10px] font-semibold text-white shadow">
                            <SparklesIcon className="h-3 w-3" />
                            AI Pick
                          </span>
                        )}
                      </div>

                      <div className="space-y-2 p-3.5 text-xs">
                        <div className="flex flex-wrap items-center gap-1">
                          <span className="rounded-full bg-[#F4EFE8] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-[#544B43]">
                            {a.platform || "media"}
                          </span>
                          <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${badge.className}`}>
                            {badge.label}
                          </span>
                        </div>

                        <div>
                          <div className="truncate font-semibold text-[#181614]" title={a.asset_id}>
                            {friendlyFileName(a)}
                          </div>
                          <div className="mt-0.5 text-[11px] text-[#877D73]">
                            {a.width && a.height ? `${a.width}×${a.height} · ` : ""}
                            {a.bytes ? `${Math.round(a.bytes / 1024)} KB` : "0 B"}
                          </div>
                        </div>

                        {imgRec && (
                          <div className="space-y-0.5 rounded-xl border border-teal-200/80 bg-teal-50/70 p-2.5 text-[11px] text-teal-950">
                            <div className="font-semibold">
                              AI Selected · {imgRec.recommended_section || "gallery"}
                              {imgRec.hero_rank ? ` · Hero #${imgRec.hero_rank}` : ""}
                              {typeof imgRec.overall_score === "number" ? ` · ${imgRec.overall_score}/100` : ""}
                            </div>
                            {imgRec.focal_point && (
                              <div className="text-teal-900">
                                Focus:{" "}
                                {typeof imgRec.focal_point === "string"
                                  ? imgRec.focal_point
                                  : `(${imgRec.focal_point.x}, ${imgRec.focal_point.y})`}
                              </div>
                            )}
                            {imgRec.crop_guidance && <div className="text-teal-900">Crop: {imgRec.crop_guidance}</div>}
                            {imgRec.reason && <div className="text-teal-900">{imgRec.reason}</div>}
                          </div>
                        )}
                        {vidRec && (
                          <div className="space-y-0.5 rounded-xl border border-indigo-200/80 bg-indigo-50/70 p-2.5 text-[11px] text-indigo-950">
                            <div className="font-semibold">
                              AI Video · {vidRec.recommended_section || "video"}
                              {typeof (vidRec.overall_score ?? vidRec.quality_score) === "number"
                                ? ` · ${vidRec.overall_score ?? vidRec.quality_score}/100`
                                : ""}
                            </div>
                            {vidRec.video_review_status && <div>Review: {vidRec.video_review_status}</div>}
                            {vidRec.reason && <div className="text-indigo-900">{vidRec.reason}</div>}
                          </div>
                        )}
                        {rejRec && !isSelected && (
                          <div className="rounded-xl bg-[#F5F1EB] p-2 text-[11px] text-[#6B635B]">
                            AI Rejected: {rejRec.reason || (rejRec.reasons || []).join(", ") || "lower score"}
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="border-t border-[#F0E9E0] bg-[#FCFBF9] px-3.5 py-2.5 text-xs">
                      <div className="mb-2 flex items-center justify-between text-[11px]">
                        <span className="text-[#7A7168]">Rights:</span>
                        <span className={isVerified ? "font-semibold text-emerald-700" : "font-medium text-amber-800"}>
                          {ledgerRight
                            ? `Verified (${ledgerRight.evidence_ref})`
                            : a.rights_status ?? "unverified"}
                        </span>
                      </div>
                      {rights && downloaded && (
                        <div className="flex flex-wrap gap-1.5">
                          <button
                            type="button"
                            disabled={rightsBusy === a.asset_id}
                            onClick={() => recordAssetRights(a.asset_id, "verified")}
                            className="flex-1 rounded-lg border border-emerald-300 bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-800 transition hover:bg-emerald-100 disabled:opacity-50"
                          >
                            Verify
                          </button>
                          <button
                            type="button"
                            disabled={rightsBusy === a.asset_id}
                            onClick={() => recordAssetRights(a.asset_id, "rejected")}
                            className="flex-1 rounded-lg border border-rose-200 bg-rose-50 px-2.5 py-1 text-[11px] font-semibold text-rose-800 transition hover:bg-rose-100 disabled:opacity-50"
                          >
                            Reject
                          </button>
                          {ledgerRight && (
                            <button
                              type="button"
                              disabled={rightsBusy === a.asset_id}
                              onClick={() => recordAssetRights(a.asset_id, "revoked")}
                              className="rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-1 text-[11px] font-semibold text-amber-900 transition hover:bg-amber-100 disabled:opacity-50"
                            >
                              Revoke
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>

            {filteredAssets.length > visibleCount && (
              <div className="flex items-center justify-center gap-3 pt-2 text-sm">
                <button
                  type="button"
                  onClick={() => setVisibleCount((c) => c + 24)}
                  className="studio-btn-secondary"
                >
                  Load 24 more ({filteredAssets.length - visibleCount} remaining)
                </button>
                <button
                  type="button"
                  onClick={() => setVisibleCount(filteredAssets.length)}
                  className="text-sm font-semibold text-[#0F766E] hover:underline"
                >
                  Show all ({filteredAssets.length})
                </button>
              </div>
            )}
          </>
        ) : (
          <div className="rounded-xl border border-dashed border-[#DDD4C8] py-10 text-center text-sm text-[#7A7168]">
            No assets match the current filter.
          </div>
        )}
      </section>

      {/* Google Drive Delivery */}
      <section className="studio-card space-y-5 p-5 sm:p-6">
        <div className="flex items-start gap-3 border-b border-[#F0E9E0] pb-4">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-emerald-50 text-emerald-700">
            <FolderCloudIcon className="h-5 w-5" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-[#181614]">Google Drive Delivery</h2>
            <p className="text-xs text-[#7A7168]">
              Creates a private client folder with organized Best Images, Best Videos, and AI Reports.
            </p>
          </div>
        </div>

        {!delivery && (
          <div className="studio-card-soft px-4 py-3 text-sm text-[#6B635B]">
            No delivery recorded yet. Verify commercial rights above when you&apos;re ready to export.
          </div>
        )}

        {delivery && (
          <div className="space-y-4 rounded-2xl border border-emerald-200/80 bg-emerald-50/40 p-4 text-sm">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-semibold text-emerald-900">
                    Status: {delivery.status}
                  </span>
                  <span className="rounded-full bg-white px-2.5 py-0.5 text-xs font-medium text-[#544B43] ring-1 ring-[#E4DCD1]">
                    Private · Restricted Sharing
                  </span>
                </div>
                {delivery.drive_folder_id && (
                  <div className="text-xs text-[#6B635B]">
                    Folder ID: <span className="font-mono">{delivery.drive_folder_id}</span>
                  </div>
                )}
              </div>

              {delivery.drive_folder_link && (
                <a
                  className="studio-btn-primary"
                  href={delivery.drive_folder_link}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open Google Drive folder
                  <ArrowRightIcon className="h-4 w-4" />
                </a>
              )}
            </div>

            {delivery.subfolders && Object.keys(delivery.subfolders).length > 0 && (
              <div className="flex flex-wrap gap-2 text-xs">
                {Object.entries(delivery.subfolders).map(([subName, subId]) => (
                  <a
                    key={subName}
                    href={`https://drive.google.com/drive/folders/${encodeURIComponent(subId)}`}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1.5 rounded-xl border border-[#DDD4C8] bg-white px-3 py-1.5 font-semibold text-[#0F766E] shadow-xs transition hover:border-[#0F766E] hover:bg-teal-50/40"
                  >
                    <FolderCloudIcon className="h-3.5 w-3.5" />
                    {subName}/
                  </a>
                ))}
              </div>
            )}

            <div className="grid gap-3 md:grid-cols-2">
              {delivery.uploaded && delivery.uploaded.length > 0 && (
                <div className="rounded-xl border border-[#E6DDD2] bg-white p-3.5">
                  <div className="text-xs font-semibold text-[#181614]">Media files uploaded</div>
                  <ul className="mt-2 space-y-1.5">
                    {delivery.uploaded.map((u) => (
                      <li key={u.asset_id} className="truncate font-mono text-xs text-[#4B433B]">
                        {u.target_subfolder ? `${u.target_subfolder}/` : ""}
                        {u.filename || u.asset_id}: <strong className="text-emerald-700">{u.status}</strong>{" "}
                        {u.drive_link ? (
                          <a href={u.drive_link} target="_blank" rel="noreferrer" className="text-[#0F766E] underline">
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
                </div>
              )}

              {delivery.reports_uploaded && delivery.reports_uploaded.length > 0 && (
                <div className="rounded-xl border border-[#E6DDD2] bg-white p-3.5">
                  <div className="text-xs font-semibold text-[#181614]">
                    Client AI &amp; Inventory reports ({delivery.reports_status || "recorded"})
                  </div>
                  <ul className="mt-2 space-y-1.5">
                    {delivery.reports_uploaded.map((r) => (
                      <li key={r.report_id || r.filename} className="truncate font-mono text-xs text-[#4B433B]">
                        {r.target_subfolder || "Reports"}/{r.filename || r.report_id}:{" "}
                        <strong className="text-emerald-700">{r.status}</strong>{" "}
                        {r.drive_link ? (
                          <a href={r.drive_link} target="_blank" rel="noreferrer" className="text-[#0F766E] underline">
                            ({r.drive_file_id})
                          </a>
                        ) : r.drive_file_id ? (
                          `(${r.drive_file_id})`
                        ) : (
                          ""
                        )}{" "}
                        {r.error ? `— ${r.error}` : ""}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>

            {delivery.pending_rights_ids && delivery.pending_rights_ids.length > 0 && (
              <div className="text-xs font-medium text-amber-800">
                Pending rights approval: {delivery.pending_rights_ids.join(", ")}
              </div>
            )}
            {delivery.errors && delivery.errors.length > 0 && (
              <div className="text-xs font-medium text-rose-700">Errors: {delivery.errors.join("; ")}</div>
            )}
          </div>
        )}

        {selection && !mock && (
          <div className="studio-card-soft space-y-4 p-4 text-xs">
            <div className="font-semibold text-[#181614]">
              Deliver Verified Media to Private Google Drive ({verifiedSelectedCount}/{selectedIds.length} AI-selected
              assets rights-verified)
            </div>
            <label className="block max-w-md font-medium text-[#4B433B]">
              Business / Folder name
              <input
                type="text"
                maxLength={120}
                value={businessName}
                onChange={(e) => setBusinessName(e.target.value)}
                className="studio-input mt-1.5"
              />
            </label>
            <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-[#E5DDD2] bg-white p-3.5 text-[#2C2824]">
              <input
                type="checkbox"
                checked={releaseConfirmed}
                onChange={(e) => setReleaseConfirmed(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded accent-[#0F766E]"
              />
              <span className="leading-relaxed">
                I confirm commercial release rights are verified for the approved assets and authorize private upload of
                approved images (<code>Best Images/</code>), videos (<code>Best Videos/</code>), and sanitized AI &amp;
                inventory reports (<code>Reports/</code>) to Google Drive (no public sharing).
              </span>
            </label>
            {deliveryMessage && (
              <p className="font-medium text-[#0F766E]" role="status">
                {deliveryMessage}
              </p>
            )}
            <button
              type="button"
              disabled={delivering || !releaseConfirmed}
              onClick={triggerDriveDelivery}
              className="studio-btn-primary"
            >
              <FolderCloudIcon className="h-4 w-4" />
              {delivering ? "Uploading via n8n…" : "Deliver Approved Media to Google Drive"}
            </button>
          </div>
        )}
      </section>

      {/* Collapsible Technical Diagnostics (cleanly hidden from non-technical operators, 100% auditable) */}
      <details className="studio-card p-5 text-xs">
        <summary className="cursor-pointer font-semibold text-[#544B43]">
          Technical Diagnostics &amp; Audit Metadata (optional)
        </summary>
        <div className="mt-4 space-y-4">
          <div className="grid grid-cols-1 gap-2 rounded-xl border border-[#EAE2D8] bg-[#FAF8F5] p-3 text-xs sm:grid-cols-3">
            <div>
              Preprocessing: <span className="font-mono font-semibold">{job.preprocessing?.status ?? "—"}</span>
            </div>
            <div>
              AI curation: <span className="font-mono font-semibold">{job.ai_curation?.status ?? "not_requested"}</span>
            </div>
            <div>
              Undated website media:{" "}
              <span className="font-mono font-semibold">{job.include_undated_website ? "included" : "excluded"}</span>
            </div>
          </div>

          <div>
            <h3 className="mb-1.5 font-semibold text-[#2C2824]">Source outcomes</h3>
            <pre className="max-h-72 overflow-auto rounded-xl border border-[#EAE2D8] bg-[#FAF8F5] p-3 font-mono text-[11px] text-[#4B433B]">
              {JSON.stringify(job.sources, null, 2)}
            </pre>
          </div>

          <div>
            <h3 className="mb-1.5 font-semibold text-[#2C2824]">Manifest metadata</h3>
            <pre className="max-h-60 overflow-auto rounded-xl border border-[#EAE2D8] bg-[#FAF8F5] p-3 font-mono text-[11px] text-[#4B433B]">
              {JSON.stringify(
                { counts: manifest?.counts ?? null, coverage: manifest?.coverage ?? null },
                null,
                2,
              )}
            </pre>
          </div>
        </div>
      </details>

      {/* Fullscreen Photo Lightbox Modal (mounted only on click) */}
      {lightboxAsset && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Photo preview"
          className="fixed inset-0 z-50 flex items-center justify-center bg-[#161412]/85 p-4 backdrop-blur-sm"
          onClick={() => setLightboxAsset(null)}
        >
          <div
            className="max-h-[90vh] w-full max-w-4xl overflow-hidden rounded-2xl border border-white/15 bg-[#1C1917] text-[#FAF8F5] shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-white/10 px-5 py-3.5 text-sm">
              <div className="min-w-0">
                <div className="truncate font-semibold text-white">{friendlyFileName(lightboxAsset.asset)}</div>
                <div className="text-xs text-[#A89F95]">
                  {lightboxAsset.asset.platform || "media"}
                  {lightboxAsset.asset.width && lightboxAsset.asset.height
                    ? ` · ${lightboxAsset.asset.width}×${lightboxAsset.asset.height}`
                    : ""}
                  {lightboxAsset.asset.bytes ? ` · ${Math.round(lightboxAsset.asset.bytes / 1024)} KB` : ""}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setLightboxAsset(null)}
                className="rounded-lg border border-white/15 px-3 py-1 text-xs font-medium text-white hover:bg-white/10"
              >
                Close ✕
              </button>
            </div>
            <div className="flex max-h-[75vh] items-center justify-center bg-black/50 p-4">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={lightboxAsset.href}
                alt={lightboxAsset.asset.asset_id}
                className="max-h-[70vh] w-auto rounded-lg object-contain"
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
