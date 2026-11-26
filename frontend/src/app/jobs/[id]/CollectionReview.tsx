"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { CheckIcon, RefreshIcon, ShieldCheckIcon, SparklesIcon } from "@/components/Icons";

type Action = "approve" | "deny" | "revoke" | "start";
type Review = {
  job_id: string;
  revision: number;
  etag: string;
  state: string;
  start_authorized: boolean;
  window_start_utc: string;
  window_end_utc: string;
  manifest_sha256: string;
  eligible_subset_sha256: string;
  permissions: Record<Action, boolean>;
  eligible_assets: string[];
  inherited_assets: Record<string, string>;
  sources: {
    source: string;
    discovered: number;
    downloaded: number;
    unknown_date: number;
    out_of_window: number;
    missing_children: number;
    coverage: string;
    evidence: string;
    outcomes: string[];
  }[];
  history: { actor: string; action: string; reason: string | null; revision: number; utc: string }[];
};
type Props = {
  id: string;
  includeUndatedWebsite?: boolean;
  aiStatus?: string | null;
  externalRefreshTick?: number;
  onChanged?: () => void;
};

function statePillStyle(state: string): string {
  if (state === "approved") return "border-emerald-200 bg-emerald-50 text-emerald-800";
  if (state === "denied" || state === "revoked") return "border-rose-200 bg-rose-50 text-rose-800";
  return "border-amber-200 bg-amber-50 text-amber-900";
}

function sourceTitle(source: string): string {
  if (source === "website") return "Website";
  if (source === "facebook") return "Facebook";
  if (source === "instagram") return "Instagram";
  return source;
}

const CAP_OUTCOMES = new Set([
  "page_budget",
  "budget_reached",
  "attachment_cap",
  "discovery_cap",
  "listing_cap",
  "transfer_budget",
  "request_budget",
  "time_budget",
  "byte_budget",
  "global_budget",
]);

function humanizeCoverageStatus(s: Review["sources"][number]): { notes: string[]; raw: string } {
  const rawTokens = [s.coverage, s.evidence, ...s.outcomes].filter(Boolean);
  const raw = rawTokens.join(" · ");
  const lowerOutcomes = new Set(s.outcomes.map((o) => o.toLowerCase()));
  const notes: string[] = [];

  if (lowerOutcomes.has("not_requested") || s.coverage === "not_requested") {
    return { notes: ["Not requested (optional)"], raw };
  }
  if (lowerOutcomes.has("owner_media_uploaded")) {
    notes.push("Client Facebook media uploaded");
  }
  if (lowerOutcomes.has("robots_disallowed")) {
    notes.push("Facebook CDN blocked automated downloads (robots.txt) — use Client Upload below");
  } else if (lowerOutcomes.has("login_redirect") || lowerOutcomes.has("restricted")) {
    notes.push("Platform login or access restriction encountered");
  } else if (lowerOutcomes.has("rate_limited")) {
    notes.push("Platform rate limit reached");
  } else if (lowerOutcomes.has("access_blocked") && s.downloaded === 0) {
    notes.push("Blocked by platform access policy");
  }
  if (lowerOutcomes.has("third_party_video_embed")) {
    notes.push("External YouTube/Vimeo video embed detected");
  }
  if (lowerOutcomes.has("js_rendered_fallback")) {
    notes.push("JS-rendered gallery scanned");
  } else if (lowerOutcomes.has("js_renderer_unavailable")) {
    notes.push("JS gallery renderer unavailable");
  }
  if ([...lowerOutcomes].some((o) => CAP_OUTCOMES.has(o))) {
    notes.push("Scan limit reached (click Resume Collection for more)");
  }
  if (notes.length === 0) {
    if (s.downloaded > 0) {
      notes.push("Partial sample collected (full site inventory cannot be independently verified)");
    } else if (s.discovered > 0) {
      notes.push("Metadata discovered (no files downloaded)");
    } else {
      notes.push("Coverage unverified");
    }
  }
  return { notes, raw };
}

export default function CollectionReview({
  id,
  includeUndatedWebsite = false,
  aiStatus,
  externalRefreshTick = 0,
  onChanged,
}: Props) {
  const url = `/api/jobs/${id}/collection-approval`;
  const [review, setReview] = useState<Review | null>(null);
  const [message, setMessage] = useState("");
  const [login, setLogin] = useState(false);
  const [reason, setReason] = useState("");
  const [policyReason, setPolicyReason] = useState("");
  const [consentChecked, setConsentChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const requestKey = useRef({ signature: "", key: "" });
  const latest = useRef(0);

  const load = useCallback(async () => {
    setBusy(true);
    const version = ++latest.current;
    try {
      const res = await fetch(url, { cache: "no-store" });
      const data = await res.json();
      if (latest.current !== version) return;
      setLogin(res.status === 401 || res.status === 403);
      setReview(res.ok ? data : null);
      setMessage(res.ok ? "" : res.status === 409 ? "No accepted collection revision is available." : data.error);
    } catch {
      if (latest.current === version) {
        setReview(null);
        setMessage("Review unavailable.");
      }
    } finally {
      if (latest.current === version) setBusy(false);
    }
  }, [url]);

  useEffect(() => {
    let active = true;
    const fetchPanel = async () => {
      if (active) await load();
    };
    void fetchPanel();
    return () => {
      active = false;
      latest.current += 1;
    };
  }, [load, externalRefreshTick]);

  async function decide(action: Action) {
    if (!review || busy) return;
    setBusy(true);
    const signature = JSON.stringify([id, review.etag, action, reason]);
    if (requestKey.current.signature !== signature) requestKey.current = { signature, key: crypto.randomUUID() };
    try {
      const res = await fetch(action === "start" ? `/api/jobs/${id}/ai-start` : url + "/decisions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "If-Match": '"' + review.etag + '"',
          "Idempotency-Key": requestKey.current.key,
        },
        body: JSON.stringify(action === "start" ? {} : { action, reason }),
      });
      if (!res.ok) {
        setMessage((await res.json()).error);
        if ([401, 403, 409, 412].includes(res.status)) {
          setReview(null);
          setReason("");
        }
        setLogin(res.status === 401 || res.status === 403);
        return;
      }
      setReason("");
      await load();
      onChanged?.();
    } catch {
      setMessage("Outcome unknown. Refresh the review to check the recorded decision.");
    } finally {
      setBusy(false);
    }
  }

  async function updateWebsitePolicy(nextInclude: boolean) {
    if (!review || busy || !policyReason.trim()) return;
    setBusy(true);
    const signature = JSON.stringify([id, review.etag, "website-policy", nextInclude, policyReason]);
    if (requestKey.current.signature !== signature) requestKey.current = { signature, key: crypto.randomUUID() };
    try {
      const res = await fetch(`/api/jobs/${id}/website-policy`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "If-Match": '"' + review.etag + '"',
          "Idempotency-Key": requestKey.current.key,
        },
        body: JSON.stringify({ include_undated_website: nextInclude, reason: policyReason.trim() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMessage(data.error || "Unable to update undated website policy.");
        if ([401, 403, 409, 412].includes(res.status)) setReview(null);
        setLogin(res.status === 401 || res.status === 403);
        return;
      }
      setPolicyReason("");
      await load();
      onChanged?.();
      setMessage(
        nextInclude
          ? "Undated website media included — new revision published for approval."
          : "Undated website media excluded — new revision published for approval.",
      );
    } catch {
      setMessage("Outcome unknown. Refresh the review to verify policy revision.");
    } finally {
      setBusy(false);
    }
  }

  async function executeAiCuration() {
    if (!review || busy || !consentChecked) return;
    setBusy(true);
    try {
      const res = await fetch(`/api/jobs/${id}/ai-execute`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "If-Match": '"' + review.etag + '"',
        },
        body: JSON.stringify({ confirm_processing_consent: true }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMessage(data.error || "AI execution could not start.");
        setLogin(res.status === 401 || res.status === 403);
        return;
      }
      setConsentChecked(false);
      setMessage(
        res.status === 202
          ? "AI Visual Director V2 curation queued in background. Status will refresh automatically."
          : "AI curation completed.",
      );
      onChanged?.();
    } catch {
      setMessage("AI execution request failed. Refresh job status before retrying.");
    } finally {
      setBusy(false);
    }
  }

  const websiteUnknown = review?.sources.find((s) => s.source === "website")?.unknown_date ?? 0;
  const aiActiveOrDone = ["queued", "started", "completed", "partial"].includes(String(aiStatus || ""));
  const hasDecisions = Boolean(review?.permissions.approve || review?.permissions.deny || review?.permissions.revoke);

  return (
    <section className="studio-card space-y-5 p-5 sm:p-6 text-sm">
      {/* Header — keep Refresh review as the FIRST button in DOM */}
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-[#F0E9E0] pb-4">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-teal-50 text-[#0F766E]">
            <ShieldCheckIcon className="h-5 w-5" />
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-semibold text-[#181614]">Collection review</h2>
              {review && (
                <span
                  className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium ${statePillStyle(
                    review.state,
                  )}`}
                >
                  Revision {review.revision} · {review.state}
                  {review.start_authorized && " · Start AI authorized"}
                </span>
              )}
            </div>
            <p className="mt-0.5 text-xs text-[#7A7168]">
              Independent Named Admin review ensures only verified, eligible media enters AI curation.
            </p>
          </div>
        </div>
        <button type="button" className="studio-btn-secondary py-1.5 text-xs" disabled={busy} onClick={load}>
          <RefreshIcon className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
          Refresh review
        </button>
      </div>

      {message && (
        <div
          role="status"
          className="rounded-xl border border-[#E5DDD2] bg-[#FAF7F2] px-4 py-2.5 text-xs font-medium text-[#2C2824]"
        >
          {message}
        </div>
      )}

      {login && (
        <div className="flex items-center justify-between rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-950">
          <span>Named Admin sign-in is required to inspect or approve this collection.</span>
          <Link href="/login" className="font-semibold text-[#0F766E] underline">
            Sign in as a named Admin
          </Link>
        </div>
      )}

      {review && (
        <>
          {/* Source Summary Table */}
          <div className="overflow-hidden rounded-xl border border-[#EAE2D8]">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <caption className="border-b border-[#F0E9E0] bg-[#FAF8F5] px-4 py-2.5 text-left text-xs font-semibold text-[#544B43]">
                  Media records in the reviewed revision ({review.eligible_assets.length} eligible for AI)
                </caption>
                <thead className="bg-[#FCFBF9] text-[11px] uppercase tracking-wider text-[#877D73]">
                  <tr>
                    {["Source", "Discovered", "Downloaded", "Unknown date", "Outside window", "Missing children", "Coverage & status"].map(
                      (t) => (
                        <th key={t} className="px-3.5 py-2.5 font-semibold">
                          {t}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#F2ECE4] bg-white">
                  {review.sources.map((s) => (
                    <tr key={s.source} className="hover:bg-[#FCFBF9]">
                      <td className="px-3.5 py-2.5 font-semibold text-[#181614]">{sourceTitle(s.source)}</td>
                      <td className="px-3.5 py-2.5 tabular-nums">{s.discovered}</td>
                      <td className="px-3.5 py-2.5 font-semibold tabular-nums text-[#0F766E]">{s.downloaded}</td>
                      <td className="px-3.5 py-2.5 tabular-nums text-[#6B635B]">{s.unknown_date}</td>
                      <td className="px-3.5 py-2.5 tabular-nums text-[#6B635B]">{s.out_of_window}</td>
                      <td className="px-3.5 py-2.5 tabular-nums text-[#6B635B]">{s.missing_children}</td>
                      <td className="px-3.5 py-2.5 text-[#544B43]">
                        {(() => {
                          const { notes, raw } = humanizeCoverageStatus(s);
                          return (
                            <div className="space-y-1">
                              <div>{notes.join(" · ")}</div>
                              {raw && (
                                <details className="text-[11px] text-[#877D73]">
                                  <summary className="cursor-pointer hover:text-[#544B43]">Technical details</summary>
                                  <div className="mt-0.5 font-mono text-[10px] leading-relaxed">{raw}</div>
                                </details>
                              )}
                            </div>
                          );
                        })()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Undated Website Media Policy Toggle */}
          {websiteUnknown > 0 && (
            <div className="space-y-3 rounded-xl border border-amber-200 bg-amber-50/75 p-4 text-xs">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="font-semibold text-amber-950">
                  Undated website media ({websiteUnknown} item{websiteUnknown === 1 ? "" : "s"} without verified publication dates)
                </div>
                <span className="rounded-full bg-white px-2.5 py-0.5 text-[11px] font-semibold text-amber-900 ring-1 ring-amber-200">
                  {includeUndatedWebsite ? "Included (labeled unverified)" : "Currently excluded"}
                </span>
              </div>
              <p className="leading-relaxed text-amber-900">
                Many salon websites don&apos;t publish upload dates on gallery photos. Current policy:{" "}
                <strong>
                  {includeUndatedWebsite ? "Included in AI curation (labeled unverified)" : "Excluded from AI curation"}
                </strong>
                . Updating this policy publishes a new immutable revision for approval.
              </p>
              <div className="flex flex-wrap gap-1.5">
                {["Include evergreen salon website gallery", "Verified current client website media"].map((preset) => (
                  <button
                    key={preset}
                    type="button"
                    onClick={() => setPolicyReason(preset)}
                    className="rounded-full border border-amber-300 bg-white px-2.5 py-0.5 text-[11px] font-medium text-amber-900 transition hover:bg-amber-100"
                  >
                    + {preset}
                  </button>
                ))}
              </div>
              <div className="flex flex-col gap-2 sm:flex-row">
                <input
                  type="text"
                  maxLength={300}
                  value={policyReason}
                  onChange={(e) => setPolicyReason(e.target.value)}
                  placeholder="Reason for website undated-media policy change"
                  className="studio-input flex-1 py-2 text-xs"
                />
                <button
                  type="button"
                  disabled={busy || !policyReason.trim()}
                  onClick={() => updateWebsitePolicy(!includeUndatedWebsite)}
                  className="shrink-0 rounded-xl border border-amber-400 bg-white px-4 py-2 text-xs font-semibold text-amber-950 shadow-sm transition hover:bg-amber-100 disabled:opacity-50"
                >
                  {includeUndatedWebsite ? "Exclude undated website media" : "Include undated website media"}
                </button>
              </div>
            </div>
          )}

          {/* Submitter / Zero-Eligible Guidance Banner */}
          {review.state === "pending" && !review.permissions.approve && (
            <div className="rounded-xl border border-sky-200 bg-sky-50/80 px-4 py-3 text-xs leading-relaxed text-sky-950">
              {review.eligible_assets.length === 0 ? (
                <>
                  <strong className="font-semibold">No eligible assets in Revision {review.revision} yet:</strong>{" "}
                  Upload client Facebook media below or include undated website media above to publish an eligible
                  revision for approval.
                </>
              ) : (
                <>
                  <strong className="font-semibold">Independent Second Admin Required:</strong>{" "}
                  You submitted or updated Revision {review.revision}. Under Two-Admin M4 governance, a{" "}
                  <strong>second Named Admin</strong> must sign in to approve this revision and launch AI selection.
                </>
              )}
            </div>
          )}

          {/* Decision Panel */}
          {hasDecisions && (
            <div className="studio-card-soft space-y-3 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs font-semibold text-[#181614]">Admin Review Decision</span>
                <div className="flex flex-wrap gap-1.5">
                  {["Approved after reviewing collected assets", "Approved by independent reviewer"].map((preset) => (
                    <button
                      key={preset}
                      type="button"
                      onClick={() => setReason(preset)}
                      className="rounded-full border border-[#DFD6CA] bg-white px-2.5 py-0.5 text-[11px] font-medium text-[#5C534B] hover:border-[#0F766E] hover:text-[#0F766E]"
                    >
                      + {preset}
                    </button>
                  ))}
                </div>
              </div>
              <label className="block text-xs font-medium text-[#5C534B]">
                Decision reason
                <textarea
                  maxLength={900}
                  rows={2}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Enter a short note explaining your approval, denial, or revocation…"
                  className="studio-input mt-1.5"
                />
              </label>
              <div className="flex flex-wrap items-center gap-2">
                {(["approve", "deny", "revoke"] as const)
                  .filter((a) => review.permissions[a])
                  .map((a) => (
                    <button
                      key={a}
                      type="button"
                      disabled={busy || !reason.trim()}
                      onClick={() => decide(a)}
                      className={
                        a === "approve"
                          ? "studio-btn-primary py-2 text-xs"
                          : "studio-btn-secondary py-2 text-xs text-rose-700 hover:border-rose-300 hover:bg-rose-50"
                      }
                    >
                      {a === "approve" && <CheckIcon className="h-3.5 w-3.5" />}
                      {a[0].toUpperCase() + a.slice(1)}
                    </button>
                  ))}
              </div>
            </div>
          )}

          {/* Authorize Start AI */}
          {review.permissions.start && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-teal-200 bg-teal-50/70 p-4">
              <div className="text-xs text-teal-950">
                <div className="font-semibold">Ready to authorize AI visual selection</div>
                <p className="mt-0.5 text-teal-900">
                  Unlock AI Visual Director V2 for Revision {review.revision} ({review.eligible_assets.length} eligible assets).
                </p>
              </div>
              <button type="button" className="studio-btn-primary" disabled={busy} onClick={() => decide("start")}>
                <SparklesIcon className="h-4 w-4" />
                Start AI
              </button>
            </div>
          )}

          {/* Execute AI Visual Director V2 */}
          {review.start_authorized && review.state === "approved" && !aiActiveOrDone && (
            <div className="space-y-3 rounded-xl border border-teal-200 bg-gradient-to-br from-teal-50/90 to-emerald-50/50 p-4 text-xs">
              <div className="flex items-center gap-2 font-semibold text-teal-950">
                <SparklesIcon className="h-4 w-4 text-[#0F766E]" />
                Execute AI Visual Director V2 (Groq Vision)
              </div>
              <p className="leading-relaxed text-teal-900">
                Start AI is authorized for Revision {review.revision} ({review.eligible_assets.length} eligible asset
                {review.eligible_assets.length === 1 ? "" : "s"}). Execution runs automatically in the background and
                updates this page when complete.
              </p>
              <label className="flex cursor-pointer items-start gap-2.5 rounded-lg bg-white/80 p-3 text-teal-950 ring-1 ring-teal-200/70">
                <input
                  type="checkbox"
                  checked={consentChecked}
                  onChange={(e) => setConsentChecked(e.target.checked)}
                  className="mt-0.5 h-4 w-4 rounded accent-[#0F766E]"
                />
                <span>I confirm AI processing consent for all reviewed eligible assets in Revision {review.revision}.</span>
              </label>
              <button
                type="button"
                disabled={busy || !consentChecked || review.eligible_assets.length === 0}
                onClick={executeAiCuration}
                className="studio-btn-primary"
              >
                <SparklesIcon className="h-4 w-4" />
                Run AI Curation (Async)
              </button>
            </div>
          )}

          {/* Collapsible Technical Audit Fingerprints & Eligible Asset List */}
          <div className="grid gap-3 md:grid-cols-2">
            <details className="rounded-xl border border-[#EAE2D8] bg-[#FAF8F5] px-4 py-3 text-xs">
              <summary className="cursor-pointer font-semibold text-[#544B43]">
                Eligible assets ({review.eligible_assets.length})
              </summary>
              <p className="mt-2 break-all font-mono text-[11px] text-[#6B635B]">
                {review.eligible_assets.join(", ") || "None"}
              </p>
              {Object.entries(review.inherited_assets).map(([aid, rep]) => (
                <p key={aid} className="mt-1 text-[11px] text-[#6B635B]">
                  {aid} inherits analysis from {rep}
                </p>
              ))}
            </details>

            <details className="rounded-xl border border-[#EAE2D8] bg-[#FAF8F5] px-4 py-3 text-xs">
              <summary className="cursor-pointer font-semibold text-[#544B43]">
                Audit hashes &amp; 365-day window
              </summary>
              <dl className="mt-2 grid gap-1.5 break-all text-[11px] text-[#6B635B]">
                <div>
                  <dt className="font-semibold text-[#3C3630]">Job ID</dt>
                  <dd className="font-mono">{review.job_id}</dd>
                </div>
                <div>
                  <dt className="font-semibold text-[#3C3630]">Frozen UTC window</dt>
                  <dd>
                    {review.window_start_utc} through {review.window_end_utc}
                  </dd>
                </div>
                <div>
                  <dt className="font-semibold text-[#3C3630]">Manifest SHA-256</dt>
                  <dd className="font-mono">{review.manifest_sha256}</dd>
                </div>
                <div>
                  <dt className="font-semibold text-[#3C3630]">Eligible subset SHA-256</dt>
                  <dd className="font-mono">{review.eligible_subset_sha256}</dd>
                </div>
              </dl>
            </details>
          </div>

          {/* Approval History */}
          <details className="rounded-xl border border-[#EAE2D8] bg-white px-4 py-3 text-xs" open={review.history.length > 0 && review.history.length <= 6}>
            <summary className="flex cursor-pointer items-center justify-between font-semibold text-[#181614]">
              <span>Approval history ({review.history.length})</span>
              <a
                href={url + "?export=audit"}
                download
                onClick={(e) => e.stopPropagation()}
                className="font-medium text-[#0F766E] hover:underline"
              >
                Export audit history
              </a>
            </summary>
            <div className="mt-3">
              {review.history.length === 0 ? (
                <p className="text-[#7A7168]">No decisions recorded yet.</p>
              ) : (
                <ol className="space-y-2 border-l-2 border-[#EAE2D8] pl-3.5">
                  {review.history.map((e, i) => (
                    <li key={i} className="relative text-[#544B43]">
                      <span className="absolute -left-[19px] top-1.5 h-2 w-2 rounded-full bg-[#0F766E]" />
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="rounded bg-[#F4EFE8] px-1.5 py-0.5 font-semibold uppercase text-[10px] text-[#2C2824]">
                          {e.action}
                        </span>
                        <span className="font-medium text-[#181614]">Rev {e.revision}</span>
                        <span>·</span>
                        <time className="text-[11px] text-[#7A7168]">{e.utc}</time>
                        <span>·</span>
                        <span className="text-[11px] text-[#7A7168]">
                          Admin <code className="font-mono">{e.actor.slice(0, 12)}…</code>
                        </span>
                      </div>
                      {e.reason && <p className="mt-0.5 text-[#3C3630]">{e.reason}</p>}
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </details>
        </>
      )}
    </section>
  );
}
