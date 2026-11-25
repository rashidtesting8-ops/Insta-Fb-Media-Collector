"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

type Action = "approve" | "deny" | "revoke" | "start";
type Review = {
  job_id: string; revision: number; etag: string; state: string; start_authorized: boolean;
  window_start_utc: string; window_end_utc: string; manifest_sha256: string; eligible_subset_sha256: string;
  permissions: Record<Action, boolean>; eligible_assets: string[]; inherited_assets: Record<string, string>;
  sources: { source: string; discovered: number; downloaded: number; unknown_date: number; out_of_window: number;
    missing_children: number; coverage: string; evidence: string; outcomes: string[] }[];
  history: { actor: string; action: string; reason: string | null; revision: number; utc: string }[];
};
type Props = {
  id: string;
  includeUndatedWebsite?: boolean;
  aiStatus?: string | null;
  onChanged?: () => void;
};
const button = "rounded border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-50";

export default function CollectionReview({ id, includeUndatedWebsite = false, aiStatus, onChanged }: Props) {
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
      if (latest.current === version) { setReview(null); setMessage("Review unavailable."); }
    } finally { if (latest.current === version) setBusy(false); }
  }, [url]);
  useEffect(() => {
    let active = true;
    const fetchPanel = async () => { if (active) await load(); };
    void fetchPanel();
    return () => { active = false; latest.current += 1; };
  }, [load]);

  async function decide(action: Action) {
    if (!review || busy) return;
    setBusy(true);
    const signature = JSON.stringify([id, review.etag, action, reason]);
    if (requestKey.current.signature !== signature) requestKey.current = { signature, key: crypto.randomUUID() };
    try {
      const res = await fetch(action === "start" ? `/api/jobs/${id}/ai-start` : url + "/decisions", {
        method: "POST", headers: { "Content-Type": "application/json", "If-Match": '"' + review.etag + '"',
          "Idempotency-Key": requestKey.current.key }, body: JSON.stringify(action === "start" ? {} : { action, reason }),
      });
      if (!res.ok) {
        setMessage((await res.json()).error);
        if ([401, 403, 409, 412].includes(res.status)) { setReview(null); setReason(""); }
        setLogin(res.status === 401 || res.status === 403);
        return;
      }
      setReason("");
      await load();
      onChanged?.();
    } catch { setMessage("Outcome unknown. Refresh the review to check the recorded decision."); }
    finally { setBusy(false); }
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

  return <section className="space-y-3 rounded-lg border border-slate-200 bg-white p-4 text-sm">
    <div className="flex items-center justify-between"><h2 className="font-medium">Collection review</h2>
      <button className={button} disabled={busy} onClick={load}>Refresh review</button></div>
    {message && <p role="status">{message}</p>}
    {login && <Link href="/login" className="text-blue-600 underline">Sign in as a named Admin</Link>}
    {review && <>
      <p>Revision {review.revision} · {review.state}{review.start_authorized && " · Start AI authorized"}</p>
      <dl className="grid gap-1 break-all">
        <dt>Job ID</dt><dd className="font-mono">{review.job_id}</dd>
        <dt>Frozen UTC window</dt><dd>{review.window_start_utc} through {review.window_end_utc}</dd>
        <dt>Manifest SHA-256</dt><dd className="font-mono">{review.manifest_sha256}</dd>
        <dt>Eligible subset SHA-256</dt><dd className="font-mono">{review.eligible_subset_sha256}</dd>
      </dl>
      <div className="overflow-x-auto"><table className="w-full text-left text-xs">
        <caption className="text-left">Media records in the reviewed revision</caption>
        <thead><tr>{["Source", "Discovered", "Downloaded", "Unknown date", "Outside window", "Missing children", "Coverage and gaps"].map(t => <th key={t} className="p-1">{t}</th>)}</tr></thead>
        <tbody>{review.sources.map(s => <tr key={s.source} className="border-t">
          {[s.source, s.discovered, s.downloaded, s.unknown_date, s.out_of_window, s.missing_children,
            [s.coverage, s.evidence, ...s.outcomes].join(" · ")].map((v, i) => <td key={i} className="p-1">{v}</td>)}
        </tr>)}</tbody></table></div>
      <details><summary>Eligible assets ({review.eligible_assets.length})</summary>
        <p className="break-all">{review.eligible_assets.join(", ") || "None"}</p>
        {Object.entries(review.inherited_assets).map(([aid, rep]) => <p key={aid}>{aid} inherits analysis from {rep}</p>)}</details>

      {websiteUnknown > 0 && (
        <div className="space-y-2 rounded border border-amber-200 bg-amber-50 p-3 text-xs">
          <div className="font-medium text-amber-900">
            Undated website media ({websiteUnknown} item{websiteUnknown === 1 ? "" : "s"} without verified publication dates)
          </div>
          <p className="text-amber-800">
            Current policy: <strong>{includeUndatedWebsite ? "Included in AI curation (labeled unverified)" : "Excluded from AI curation"}</strong>.
            Changing this policy publishes a new immutable revision for approval.
          </p>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              type="text"
              maxLength={300}
              value={policyReason}
              onChange={(e) => setPolicyReason(e.target.value)}
              placeholder="Reason for website undated-media policy change"
              className="flex-1 rounded border border-amber-300 bg-white px-2 py-1 text-xs"
            />
            <button
              type="button"
              disabled={busy || !policyReason.trim()}
              onClick={() => updateWebsitePolicy(!includeUndatedWebsite)}
              className="rounded border border-amber-400 bg-white px-3 py-1 font-medium text-amber-900 hover:bg-amber-100 disabled:opacity-50"
            >
              {includeUndatedWebsite ? "Exclude undated website media" : "Include undated website media"}
            </button>
          </div>
        </div>
      )}

      {(review.permissions.approve || review.permissions.deny || review.permissions.revoke) &&
        <label className="block">Decision reason<textarea maxLength={900} value={reason} onChange={e => setReason(e.target.value)}
          className="block w-full rounded border border-slate-300 p-2" /></label>}
      <div className="flex flex-wrap gap-2">{(["approve", "deny", "revoke"] as const).filter(a => review.permissions[a]).map(a =>
        <button className={button} key={a} disabled={busy || !reason.trim()} onClick={() => decide(a)}>{a[0].toUpperCase()+a.slice(1)}</button>)}</div>
      {review.permissions.start && <button className="rounded bg-blue-600 px-3 py-2 text-white" disabled={busy} onClick={() => decide("start")}>Start AI</button>}

      {review.start_authorized && review.state === "approved" && !aiActiveOrDone && (
        <div className="space-y-2 rounded border border-blue-200 bg-blue-50 p-3 text-xs">
          <div className="font-medium text-blue-900">Execute AI Visual Director V2 (Groq Vision)</div>
          <p className="text-blue-800">
            Start AI is authorized for Revision {review.revision} ({review.eligible_assets.length} eligible asset
            {review.eligible_assets.length === 1 ? "" : "s"}). Execution runs asynchronously in the worker.
          </p>
          <label className="flex items-start gap-2 text-blue-900">
            <input
              type="checkbox"
              checked={consentChecked}
              onChange={(e) => setConsentChecked(e.target.checked)}
              className="mt-0.5"
            />
            <span>I confirm AI processing consent for all reviewed eligible assets in Revision {review.revision}.</span>
          </label>
          <button
            type="button"
            disabled={busy || !consentChecked || review.eligible_assets.length === 0}
            onClick={executeAiCuration}
            className="rounded bg-blue-600 px-3 py-1.5 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            Run AI Curation (Async)
          </button>
        </div>
      )}

      <h3 className="font-medium">Approval history</h3>
      {review.history.length === 0 && <p>No decisions recorded.</p>}
      <ol>{review.history.map((e, i) => <li key={i} className="border-t py-1">
        <time>{e.utc}</time> · Revision {e.revision} · {e.action} · Admin <code>{e.actor}</code>{e.reason && <p>{e.reason}</p>}
      </li>)}</ol>
      <a href={url + "?export=audit"} download className="text-blue-600 underline">Export audit history</a>
    </>}
  </section>;
}
