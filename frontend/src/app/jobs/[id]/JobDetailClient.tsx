/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import CollectionReview from "./CollectionReview";
import { api } from "@/lib/api";
import type { JobRecord, PipelineStatus, Manifest, Curation, Delivery } from "@/lib/types";
import { curationSelection, isMockCuration } from "@/lib/types";

const STAGES = ["queued", "collecting", "preprocessing", "ai_processing", "awaiting_rights_approval", "delivering", "completed", "partial_success", "blocked", "failed"];

export default function JobDetailClient({ id }: { id: string }) {
  const [job, setJob] = useState<JobRecord | null>(null);
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [curation, setCuration] = useState<Curation | null>(null);
  const [delivery, setDelivery] = useState<Delivery | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function loadAll() {
      let terminal = false;
      try {
        const [j, s, m, c, d] = await Promise.all([
          api<JobRecord>(`/api/jobs/${id}`, options),
          api<PipelineStatus>(`/api/jobs/${id}/status`, options),
          api<Manifest>(`/api/jobs/${id}/manifest`, options).catch(() => null),
          api<Curation>(`/api/jobs/${id}/curation`, options).catch(() => null),
          api<Delivery>(`/api/jobs/${id}/delivery`, options).catch(() => null),
        ]);
        if (controller.signal.aborted) return;
        terminal = s.terminal;
        setError(null);
        setJob(j); setStatus(s); setManifest(m); setCuration(c); setDelivery(d);
      } catch (e: unknown) {
        if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (!terminal) timer = setTimeout(loadAll, 10000);
        }
      }
    }
    void loadAll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id]);

  if (loading) return <div className="p-4 text-sm text-slate-500">Loading…</div>;
  if (error) return <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>;
  if (!job) return <div className="p-4 text-sm text-slate-500">Job not found.</div>;

  const mock = isMockCuration(curation);
  const selection = curationSelection(curation);
  const stageIdx = STAGES.indexOf(String(status?.stage));
  const progress = stageIdx >= 0 ? Math.round(((stageIdx + 1) / STAGES.length) * 100) : 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Job {job.job_id}</h1>
        <Link href="/" className="text-sm text-blue-600 hover:underline">← Dashboard</Link>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="mb-2 flex items-center justify-between text-sm">
          <span className="font-medium">Pipeline stage: {status?.stage ?? "—"}</span>
          <span className="text-slate-500">{status?.detail}</span>
        </div>
        <div className="h-2 w-full rounded bg-slate-100">
          <div className="h-2 rounded bg-blue-600" style={{ width: `${progress}%` }} />
        </div>
        <div className="mt-2 text-xs text-slate-500">Progress indicator is stage-based, not a fabricated percentage of work.</div>
      </div>

      <CollectionReview key={id} id={id} />
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-medium">Source outcomes</h2>
        <pre className="overflow-auto rounded bg-slate-50 p-3 text-xs">{JSON.stringify(job.sources, null, 2)}</pre>
        <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
          <div>Preprocessing: <span className="font-mono">{(job.preprocessing as any)?.status ?? "—"}</span></div>
          <div>AI curation: <span className="font-mono">{(job.ai_curation as any)?.status ?? "not_requested"}</span></div>
        </div>
        {job.error && <div className="mt-3 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{job.error}</div>}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-medium">Media results</h2>
        {mock && <div className="mb-3 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">Mock/fixture curation detected — not shown as genuine AI recommendations.</div>}
        {!selection && !mock && <div className="text-sm text-slate-500">No curation available.</div>}
        {selection && (
          <div className="space-y-2 text-sm">
            <div>Mode: {selection.mode ?? curation?.status}</div>
            <div>Selected assets: {(selection.selected_asset_ids || []).length}</div>
            <div>Rights review required: {(selection.rights_review_required_ids || []).length}</div>
          </div>
        )}
        {manifest?.assets && manifest.assets.length > 0 && (
          <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
            {manifest.assets.slice(0, 24).map((a) => (
              <div key={a.asset_id} className="rounded border border-slate-200 p-2 text-xs">
                <img
                  src={`/api/jobs/${id}/file?path=${encodeURIComponent(a.local_path || "")}`}
                  alt={a.asset_id}
                  className="mb-1 aspect-square w-full rounded object-cover"
                  loading="lazy"
                />
                <div className="font-mono">{a.asset_id}</div>
                <div className="text-slate-500">{a.rights_status ?? "unverified"} · {a.bytes ?? 0} B</div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-medium">Delivery</h2>
        {!delivery && <div className="text-sm text-slate-500">No delivery recorded.</div>}
        {delivery && (
          <div className="space-y-2 text-sm">
            <div>Status: <span className="font-mono">{delivery.status}</span></div>
            {delivery.drive_folder_id && <div>Drive folder: {delivery.drive_folder_id}</div>}
            {delivery.drive_folder_link && <a className="text-blue-600 hover:underline" href={delivery.drive_folder_link} target="_blank" rel="noreferrer">Open folder</a>}
            {delivery.uploaded && delivery.uploaded.length > 0 && (
              <ul className="mt-2 space-y-1">
                {delivery.uploaded.map((u) => (
                  <li key={u.asset_id} className="font-mono text-xs">{u.asset_id}: {u.status} {u.drive_file_id ? `(${u.drive_file_id})` : ""} {u.error ? `— ${u.error}` : ""}</li>
                ))}
              </ul>
            )}
            {delivery.pending_rights_ids && delivery.pending_rights_ids.length > 0 && (
              <div className="text-amber-700">Pending rights approval: {delivery.pending_rights_ids.join(", ")}</div>
            )}
          </div>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-medium">Manifest metadata</h2>
        <pre className="overflow-auto rounded bg-slate-50 p-3 text-xs">{JSON.stringify(manifest?.counts ?? manifest?.coverage ?? {}, null, 2)}</pre>
      </section>
    </div>
  );
}
