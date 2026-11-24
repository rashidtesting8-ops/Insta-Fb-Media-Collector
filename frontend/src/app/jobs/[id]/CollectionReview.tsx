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
const button = "rounded border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-50";

export default function CollectionReview({ id }: { id: string }) {
  const url = `/api/jobs/${id}/collection-approval`;
  const [review, setReview] = useState<Review | null>(null);
  const [message, setMessage] = useState("");
  const [login, setLogin] = useState(false);
  const [reason, setReason] = useState("");
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
    } catch { setMessage("Outcome unknown. Refresh the review to check the recorded decision."); }
    finally { setBusy(false); }
  }

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
        {Object.entries(review.inherited_assets).map(([id, rep]) => <p key={id}>{id} inherits analysis from {rep}</p>)}</details>
      {(review.permissions.approve || review.permissions.deny || review.permissions.revoke) &&
        <label className="block">Decision reason<textarea maxLength={900} value={reason} onChange={e => setReason(e.target.value)}
          className="block w-full rounded border border-slate-300 p-2" /></label>}
      <div className="flex flex-wrap gap-2">{(["approve", "deny", "revoke"] as const).filter(a => review.permissions[a]).map(a =>
        <button className={button} key={a} disabled={busy || !reason.trim()} onClick={() => decide(a)}>{a[0].toUpperCase()+a.slice(1)}</button>)}</div>
      {review.permissions.start && <button className="rounded bg-blue-600 px-3 py-2 text-white" disabled={busy} onClick={() => decide("start")}>Start AI</button>}
      <h3 className="font-medium">Approval history</h3>
      {review.history.length === 0 && <p>No decisions recorded.</p>}
      <ol>{review.history.map((e, i) => <li key={i} className="border-t py-1">
        <time>{e.utc}</time> · Revision {e.revision} · {e.action} · Admin <code>{e.actor}</code>{e.reason && <p>{e.reason}</p>}
      </li>)}</ol>
      <a href={url + "?export=audit"} download className="text-blue-600 underline">Export audit history</a>
    </>}
  </section>;
}
