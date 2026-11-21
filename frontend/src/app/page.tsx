"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { JobSummary } from "@/lib/types";

export default function Dashboard() {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const data = await api<{ jobs: JobSummary[]; total: number }>("/api/jobs");
      setJobs(data.jobs || []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const total = jobs.length;
  const running = jobs.filter((j) => ["queued", "running"].includes(j.status)).length;
  const completed = jobs.filter((j) => j.status === "extraction_complete" || j.status === "completed").length;
  const failed = jobs.filter((j) => ["failed", "partial_success", "interrupted", "blocked"].includes(j.status)).length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <button onClick={load} className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100">Refresh</button>
      </div>
      {error && <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {[
          ["Total submitted", total],
          ["Running", running],
          ["Completed", completed],
          ["Failed / blocked", failed],
        ].map(([label, val]) => (
          <div key={label as string} className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
            <div className="mt-2 text-3xl font-semibold">{loading ? "…" : val}</div>
          </div>
        ))}
      </div>
      <div className="rounded-lg border border-slate-200 bg-white">
        <div className="border-b border-slate-200 px-4 py-3 font-medium">Recent job activity</div>
        {loading ? (
          <div className="p-4 text-sm text-slate-500">Loading…</div>
        ) : jobs.length === 0 ? (
          <div className="p-4 text-sm text-slate-500">No jobs yet. Start a new collection.</div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {jobs.slice(0, 10).map((j) => (
              <li key={j.job_id} className="flex items-center justify-between px-4 py-3 text-sm">
                <a href={`/jobs/${j.job_id}`} className="font-mono text-blue-600 hover:underline">{j.job_id}</a>
                <span className="text-slate-500">{j.website_url}</span>
                <span className="rounded bg-slate-100 px-2 py-0.5 text-xs">{j.status}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
