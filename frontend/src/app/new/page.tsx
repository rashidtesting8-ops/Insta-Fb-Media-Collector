"use client";
import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { sourceUrlErrors } from "@/lib/intake";

export default function NewCollection() {
  const router = useRouter();
  const [form, setForm] = useState({
    business_name: "",
    website_url: "",
    instagram_url: "",
    facebook_url: "",
    max_pages: "10",
    max_media: "30",
    max_social_files: "20",
    max_selected_images: "30",
    max_selected_videos: "10",
    social_permission_confirmed: false,
    visual_preferences: "",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const previousSubmission = useRef<{ payload: string; key: string } | null>(null);

  function validate() {
    const e = sourceUrlErrors(form);
    const mp = Number(form.max_pages);
    const mm = Number(form.max_media);
    const ms = Number(form.max_social_files);
    const mi = Number(form.max_selected_images);
    const mv = Number(form.max_selected_videos);
    if (!Number.isInteger(mp) || mp < 1 || mp > 25) e.max_pages = "Between 1 and 25";
    if (!Number.isInteger(mm) || mm < 1 || mm > 80) e.max_media = "Between 1 and 80";
    if (!Number.isInteger(ms) || ms < 1 || ms > 60) e.max_social_files = "Between 1 and 60";
    if (!Number.isInteger(mi) || mi < 1 || mi > 200) e.max_selected_images = "Between 1 and 200";
    if (!Number.isInteger(mv) || mv < 1 || mv > 100) e.max_selected_videos = "Between 1 and 100";
    return e;
  }

  async function onSubmit(ev: React.FormEvent) {
    ev.preventDefault();
    const e = validate();
    setErrors(e);
    if (Object.keys(e).length) return;
    setSubmitting(true);
    setServerError(null);
    try {
      const instagramTrimmed = form.instagram_url.trim();
      const payload = {
        website_url: form.website_url.trim(),
        ...(instagramTrimmed ? { instagram_url: instagramTrimmed } : {}),
        facebook_url: form.facebook_url.trim(),
        business_name: form.business_name.trim() || undefined,
        max_pages: Number(form.max_pages),
        max_media: Number(form.max_media),
        max_social_files: Number(form.max_social_files),
        max_selected_images: Number(form.max_selected_images),
        max_selected_videos: Number(form.max_selected_videos),
        run_ai_curation: false,
        social_permission_confirmed: form.social_permission_confirmed,
        user_selection_notes: form.visual_preferences.trim() || undefined,
        business_context: form.visual_preferences.trim() ? { visual_preferences: form.visual_preferences.trim() } : undefined,
      };
      const fingerprint = JSON.stringify(payload);
      if (previousSubmission.current?.payload !== fingerprint) {
        previousSubmission.current = { payload: fingerprint, key: crypto.randomUUID() };
      }
      const job = await api<{ job_id: string }>("/api/jobs", {
        method: "POST",
        body: JSON.stringify({ ...payload, idempotency_key: previousSubmission.current.key }),
      });
      router.push(`/jobs/${job.job_id}`);
    } catch (err: unknown) {
      setServerError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const field = (name: string, label: string, props: Record<string, unknown> = {}) => (
    <div>
      <label className="mb-1 block text-sm font-medium">{label}</label>
      <input
        className="w-full rounded border border-slate-300 px-3 py-2 text-sm"
        value={String((form as Record<string, unknown>)[name] ?? "")}
        onChange={(e) => setForm({ ...form, [name]: e.target.value })}
        {...props}
      />
      {errors[name] && <p className="mt-1 text-xs text-red-600">{errors[name]}</p>}
    </div>
  );

  return (
    <div className="max-w-2xl space-y-6">
      <h1 className="text-2xl font-semibold">New Media Collection</h1>
      <p className="text-sm text-slate-600">
        Automatically discovers, collects, deduplicates, and preprocesses available preceding-365-day media from the
        business Website and Facebook Page. AI curation and private Google Drive delivery run from the Job page after
        independent Named Admin review. Instagram is optional (deferred in V2.1).
      </p>
      <form onSubmit={onSubmit} className="space-y-4 rounded-lg border border-slate-200 bg-white p-6">
        {field("business_name", "Business name")}
        {field("website_url", "Business website URL *", { required: true, placeholder: "https://example.com" })}
        {field("facebook_url", "Facebook Page URL *", { required: true, placeholder: "https://www.facebook.com/..." })}
        {field("instagram_url", "Instagram URL (optional — deferred in V2.1)", { placeholder: "https://www.instagram.com/..." })}
        <div className="grid grid-cols-2 gap-4">
          {field("max_selected_images", "Max AI-selected images")}
          {field("max_selected_videos", "Max AI-selected videos")}
        </div>
        <details className="rounded border border-slate-200 bg-slate-50 p-3 text-sm">
          <summary className="cursor-pointer font-medium text-slate-700">Collection bounds (optional)</summary>
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
            {field("max_pages", "Max website pages (1–25)")}
            {field("max_media", "Max website media (1–80)")}
            {field("max_social_files", "Max Facebook files (1–60)")}
          </div>
        </details>
        <div>
          <label className="mb-1 block text-sm font-medium">Visual-direction preferences (optional)</label>
          <textarea
            className="w-full rounded border border-slate-300 px-3 py-2 text-sm"
            rows={3}
            value={form.visual_preferences}
            onChange={(e) => setForm({ ...form, visual_preferences: e.target.value })}
          />
        </div>
        <label className="flex items-start gap-2 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={form.social_permission_confirmed}
            onChange={(e) => setForm({ ...form, social_permission_confirmed: e.target.checked })}
          />
          <span>
            I confirm client authorization to download permitted public Facebook media for this business (leave unchecked
            for metadata-only Facebook discovery).
          </span>
        </label>
        {serverError && <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{serverError}</div>}
        <button disabled={submitting} className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50">
          {submitting ? "Starting…" : "Start Collection"}
        </button>
      </form>
    </div>
  );
}
