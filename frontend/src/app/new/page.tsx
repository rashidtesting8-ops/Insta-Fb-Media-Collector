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
    max_selected_images: "30",
    max_selected_videos: "10",
    run_ai_curation: false,
    social_permission_confirmed: false,
    visual_preferences: "",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const previousSubmission = useRef<{ payload: string; key: string } | null>(null);

  function validate() {
    const e = sourceUrlErrors(form);
    const mi = Number(form.max_selected_images);
    const mv = Number(form.max_selected_videos);
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
      const payload = {
          website_url: form.website_url.trim(),
          instagram_url: form.instagram_url.trim(),
          facebook_url: form.facebook_url.trim(),
          business_name: form.business_name.trim() || undefined,
          max_selected_images: Number(form.max_selected_images),
          max_selected_videos: Number(form.max_selected_videos),
          run_ai_curation: form.run_ai_curation,
          social_permission_confirmed: form.social_permission_confirmed,
          business_context: form.visual_preferences ? { visual_preferences: form.visual_preferences } : undefined,
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
      <form onSubmit={onSubmit} className="space-y-4 rounded-lg border border-slate-200 bg-white p-6">
        {field("business_name", "Business name")}
        {field("website_url", "Business website URL *", { required: true, placeholder: "https://example.com" })}
        {field("instagram_url", "Instagram URL *", { required: true, placeholder: "https://www.instagram.com/..." })}
        {field("facebook_url", "Facebook URL *", { required: true, placeholder: "https://www.facebook.com/..." })}
        <div className="grid grid-cols-2 gap-4">
          {field("max_selected_images", "Max images")}
          {field("max_selected_videos", "Max videos")}
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium">Visual-direction preferences (optional)</label>
          <textarea
            className="w-full rounded border border-slate-300 px-3 py-2 text-sm"
            rows={3}
            value={form.visual_preferences}
            onChange={(e) => setForm({ ...form, visual_preferences: e.target.value })}
          />
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.run_ai_curation} onChange={(e) => setForm({ ...form, run_ai_curation: e.target.checked })} />
          Run AI curation (requires AI provider configured)
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.social_permission_confirmed} onChange={(e) => setForm({ ...form, social_permission_confirmed: e.target.checked })} />
          I confirm collection/use is permitted (required for social sources)
        </label>
        {serverError && <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{serverError}</div>}
        <button disabled={submitting} className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50">
          {submitting ? "Starting…" : "Start Collection"}
        </button>
      </form>
    </div>
  );
}
