"use client";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { friendlyErrorMessage } from "@/lib/errors";
import type { JobSummary } from "@/lib/types";
import {
  ArrowRightIcon,
  FolderCloudIcon,
  GlobeIcon,
  PlusCircleIcon,
  RefreshIcon,
  SearchIcon,
  ShieldCheckIcon,
  SparklesIcon,
} from "@/components/Icons";

type StatusFilter = "all" | "active" | "completed" | "attention";

function friendlyDomain(url?: string | null): string {
  if (!url) return "Client Media Project";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function friendlyDate(iso?: string | null): string {
  if (!iso) return "";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "";
  return dt.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function friendlyJobStatus(raw: string): { label: string; dot: string; badge: string; group: StatusFilter } {
  switch (raw) {
    case "completed":
      return {
        label: "Delivered & Complete",
        dot: "bg-emerald-500",
        badge: "bg-emerald-50 text-emerald-800 border-emerald-200",
        group: "completed",
      };
    case "extraction_complete":
      return {
        label: "Ready for Review",
        dot: "bg-teal-500",
        badge: "bg-teal-50 text-teal-800 border-teal-200",
        group: "completed",
      };
    case "queued":
      return {
        label: "Queued",
        dot: "bg-sky-500 animate-pulse",
        badge: "bg-sky-50 text-sky-800 border-sky-200",
        group: "active",
      };
    case "running":
      return {
        label: "Collecting Media…",
        dot: "bg-sky-500 animate-pulse",
        badge: "bg-sky-50 text-sky-800 border-sky-200",
        group: "active",
      };
    case "partial_success":
      return {
        label: "Partial — Review Needed",
        dot: "bg-amber-500",
        badge: "bg-amber-50 text-amber-900 border-amber-200",
        group: "attention",
      };
    case "blocked":
      return {
        label: "Paused / Blocked",
        dot: "bg-amber-600",
        badge: "bg-amber-50 text-amber-900 border-amber-200",
        group: "attention",
      };
    case "failed":
    case "interrupted":
      return {
        label: raw === "failed" ? "Needs Attention" : "Interrupted",
        dot: "bg-rose-500",
        badge: "bg-rose-50 text-rose-800 border-rose-200",
        group: "attention",
      };
    default:
      return {
        label: raw.replace(/_/g, " "),
        dot: "bg-stone-400",
        badge: "bg-stone-100 text-stone-700 border-stone-200",
        group: "all",
      };
  }
}

export default function Dashboard() {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [visibleCount, setVisibleCount] = useState(20);

  async function load() {
    setRefreshing(true);
    setError(null);
    try {
      const data = await api<{ jobs: JobSummary[]; total: number }>("/api/jobs");
      setJobs(data.jobs || []);
    } catch (e: unknown) {
      setError(friendlyErrorMessage(e, "Unable to load projects from the local worker."));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    let active = true;
    api<{ jobs: JobSummary[]; total: number }>("/api/jobs")
      .then((data) => {
        if (active) {
          setError(null);
          setJobs(data.jobs || []);
        }
      })
      .catch((e: unknown) => {
        if (active) setError(friendlyErrorMessage(e, "Unable to load projects from the local worker."));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const total = jobs.length;
  const running = jobs.filter((j) => ["queued", "running"].includes(j.status)).length;
  const completed = jobs.filter((j) => j.status === "extraction_complete" || j.status === "completed").length;
  const attention = jobs.filter((j) => ["failed", "partial_success", "interrupted", "blocked"].includes(j.status)).length;

  const filteredJobs = useMemo(() => {
    const q = query.trim().toLowerCase();
    return jobs.filter((j) => {
      const meta = friendlyJobStatus(j.status);
      if (statusFilter !== "all" && meta.group !== statusFilter) return false;
      if (!q) return true;
      return (
        j.job_id.toLowerCase().includes(q) ||
        String(j.website_url || "").toLowerCase().includes(q) ||
        meta.label.toLowerCase().includes(q)
      );
    });
  }, [jobs, query, statusFilter]);

  return (
    <div className="space-y-7">
      {/* Studio Hero Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="text-xs font-semibold uppercase tracking-[0.14em] text-[#9A6B37]">
            Client Media Workspace
          </div>
          <h1 className="mt-1 font-serif text-3xl font-medium tracking-tight text-[#181614]">
            Dashboard
          </h1>
          <p className="mt-1 text-sm text-[#6B635B]">
            Track client collections, review AI-curated photos &amp; videos, and deliver verified galleries to Google Drive.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2.5">
          <button
            type="button"
            onClick={load}
            disabled={refreshing}
            className="studio-btn-secondary"
          >
            <RefreshIcon className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
          <Link href="/new" className="studio-btn-primary">
            <PlusCircleIcon className="h-4 w-4" />
            New Collection
          </Link>
        </div>
      </div>

      {error && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <span>{error}</span>
          <button type="button" onClick={load} className="studio-btn-secondary py-1.5 text-xs">
            Retry
          </button>
        </div>
      )}

      {/* KPI Metric Cards */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {[
          {
            label: "Total Projects",
            val: total,
            sub: "All client collections",
            accent: "bg-stone-100 text-stone-700",
            Icon: GlobeIcon,
          },
          {
            label: "In Progress",
            val: running,
            sub: "Actively collecting",
            accent: "bg-sky-50 text-sky-700",
            Icon: SparklesIcon,
          },
          {
            label: "Ready / Delivered",
            val: completed,
            sub: "Collected or complete",
            accent: "bg-emerald-50 text-emerald-700",
            Icon: FolderCloudIcon,
          },
          {
            label: "Needs Review",
            val: attention,
            sub: "Partial or action needed",
            accent: "bg-amber-50 text-amber-800",
            Icon: ShieldCheckIcon,
          },
        ].map(({ label, val, sub, accent, Icon }) => (
          <div key={label} className="studio-card p-5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-[#7A7168]">{label}</span>
              <span className={`flex h-8 w-8 items-center justify-center rounded-lg ${accent}`}>
                <Icon className="h-4 w-4" />
              </span>
            </div>
            <div className="mt-3 text-3xl font-semibold tracking-tight text-[#181614]">
              {loading ? <span className="inline-block h-8 w-12 animate-pulse rounded bg-[#EFEAE2]" /> : val}
            </div>
            <div className="mt-1 text-xs text-[#8A8076]">{sub}</div>
          </div>
        ))}
      </div>

      {/* Client Projects Table / Card List */}
      <section className="studio-card overflow-hidden">
        <div className="flex flex-col gap-3 border-b border-[#EDE6DC] px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-[#181614]">Recent Client Projects</h2>
            <p className="text-xs text-[#7A7168]">
              Click any project to review media, run AI curation, verify rights, or export to Google Drive.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-[200px] flex-1 sm:w-56">
              <SearchIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[#9A9086]" />
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search salon or ID…"
                aria-label="Search client projects"
                className="w-full rounded-xl border border-[#DDD4C8] bg-[#FAF8F5] py-1.5 pl-9 pr-3 text-sm text-[#181614] placeholder:text-[#9A9086] focus:border-[#0F766E] focus:bg-white focus:outline-none"
              />
            </div>
          </div>
        </div>

        {/* Filter pills */}
        <div className="flex flex-wrap items-center gap-1.5 border-b border-[#F2ECE4] bg-[#FCFBF9] px-5 py-2.5 text-xs">
          {(
            [
              ["all", `All (${total})`],
              ["active", `In Progress (${running})`],
              ["completed", `Ready / Complete (${completed})`],
              ["attention", `Needs Review (${attention})`],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setStatusFilter(key)}
              className={`rounded-full px-3 py-1 font-medium transition ${
                statusFilter === key
                  ? "bg-[#181614] text-white"
                  : "bg-white text-[#5E564E] ring-1 ring-[#E4DCD1] hover:bg-[#F4EFE8]"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {loading ? (
          <div className="divide-y divide-[#F2ECE4] p-5">
            {[1, 2, 3].map((i) => (
              <div key={i} className="flex items-center justify-between py-3.5">
                <div className="space-y-2">
                  <div className="h-4 w-44 animate-pulse rounded bg-[#EFEAE2]" />
                  <div className="h-3 w-64 animate-pulse rounded bg-[#F5F1EA]" />
                </div>
                <div className="h-7 w-28 animate-pulse rounded-full bg-[#EFEAE2]" />
              </div>
            ))}
          </div>
        ) : filteredJobs.length === 0 ? (
          <div className="px-6 py-12 text-center">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-[#F4EFE8] text-[#0F766E]">
              <SparklesIcon className="h-6 w-6" />
            </div>
            <h3 className="mt-4 text-base font-semibold text-[#181614]">
              {jobs.length === 0 ? "No client collections yet" : "No projects match your filter"}
            </h3>
            <p className="mx-auto mt-1 max-w-sm text-sm text-[#7A7168]">
              {jobs.length === 0
                ? "Start by pasting a pet grooming salon's website and Facebook Page link. We'll gather and organize their media automatically."
                : "Try clearing your search or switching the status filter above."}
            </p>
            {jobs.length === 0 && (
              <Link href="/new" className="studio-btn-primary mt-5">
                <PlusCircleIcon className="h-4 w-4" />
                Start Your First Collection
              </Link>
            )}
          </div>
        ) : (
          <>
            <ul className="divide-y divide-[#F0E9E0]">
              {filteredJobs.slice(0, visibleCount).map((j) => {
              const badge = friendlyJobStatus(j.status);
              const domain = friendlyDomain(j.website_url);
              const dateText = friendlyDate(j.created_at || j.updated_at);

              return (
                <li key={j.job_id}>
                  <Link
                    href={`/jobs/${j.job_id}`}
                    className="group flex flex-col gap-3 px-5 py-4 transition hover:bg-[#FAF7F2] sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="min-w-0 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate text-[15px] font-semibold text-[#181614] group-hover:text-[#0F766E]">
                          {domain}
                        </span>
                        <span className="rounded-md bg-[#F4EFE8] px-1.5 py-0.5 font-mono text-[11px] text-[#7A7168]">
                          #{j.job_id.slice(0, 8)}
                        </span>
                        {dateText && <span className="text-xs text-[#948A7F]">· {dateText}</span>}
                      </div>
                      <div className="truncate text-xs text-[#7A7168]">{j.website_url || j.job_id}</div>
                    </div>

                    <div className="flex shrink-0 items-center gap-3">
                      <span
                        className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${badge.badge}`}
                      >
                        <span className={`h-1.5 w-1.5 rounded-full ${badge.dot}`} />
                        {badge.label}
                      </span>
                      <span className="inline-flex items-center gap-1 text-xs font-semibold text-[#0F766E] transition group-hover:translate-x-0.5">
                        Open
                        <ArrowRightIcon className="h-3.5 w-3.5" />
                      </span>
                    </div>
                  </Link>
                </li>
              );
              })}
            </ul>
            {filteredJobs.length > visibleCount && (
              <div className="flex flex-wrap items-center justify-center gap-3 border-t border-[#F0E9E0] px-5 py-3.5 text-xs">
                <button
                  type="button"
                  onClick={() => setVisibleCount((c) => c + 20)}
                  className="studio-btn-secondary py-1.5 text-xs"
                >
                  Load 20 more ({filteredJobs.length - visibleCount} remaining)
                </button>
                <button
                  type="button"
                  onClick={() => setVisibleCount(filteredJobs.length)}
                  className="font-semibold text-[#0F766E] hover:underline"
                >
                  Show all ({filteredJobs.length})
                </button>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
