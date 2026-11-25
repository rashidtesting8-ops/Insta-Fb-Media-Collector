"use client";
import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { sourceUrlErrors } from "@/lib/intake";
import { ArrowRightIcon, CheckIcon, FolderCloudIcon, ShieldCheckIcon, SparklesIcon } from "@/components/Icons";

type SizePreset = {
  id: string;
  title: string;
  tag: string;
  description: string;
  values: {
    max_pages: string;
    max_media: string;
    max_social_files: string;
    max_selected_images: string;
    max_selected_videos: string;
  };
};

const SIZE_PRESETS: SizePreset[] = [
  {
    id: "quick",
    title: "Quick Pilot",
    tag: "~3 min",
    description: "Best for fast tests & small salons (up to 6 website items)",
    values: {
      max_pages: "4",
      max_media: "6",
      max_social_files: "3",
      max_selected_images: "4",
      max_selected_videos: "1",
    },
  },
  {
    id: "standard",
    title: "Standard Salon",
    tag: "Recommended",
    description: "Balanced 365-day collection for a typical grooming website",
    values: {
      max_pages: "10",
      max_media: "30",
      max_social_files: "20",
      max_selected_images: "30",
      max_selected_videos: "10",
    },
  },
  {
    id: "large",
    title: "Full Gallery",
    tag: "High volume",
    description: "Deep scan for established salons with extensive galleries",
    values: {
      max_pages: "20",
      max_media: "60",
      max_social_files: "40",
      max_selected_images: "40",
      max_selected_videos: "15",
    },
  },
];

const STYLE_CHIPS = [
  "Warm & luxury spa feel",
  "Bright, clean & modern",
  "Highlight happy groomed pets",
  "Include salon interior & facilities",
  "Prioritize before & after transformations",
] as const;

export default function NewCollection() {
  const router = useRouter();
  const [activePreset, setActivePreset] = useState<string>("standard");
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

  function applyPreset(preset: SizePreset) {
    setActivePreset(preset.id);
    setForm((prev) => ({ ...prev, ...preset.values }));
  }

  function appendStyleChip(chip: string) {
    setForm((prev) => {
      const current = prev.visual_preferences.trim();
      if (current.toLowerCase().includes(chip.toLowerCase())) return prev;
      return {
        ...prev,
        visual_preferences: current ? `${current}. ${chip}` : chip,
      };
    });
  }

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
        business_context: form.visual_preferences.trim()
          ? { visual_preferences: form.visual_preferences.trim() }
          : undefined,
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

  const field = (name: string, label: string, helper?: string, props: Record<string, unknown> = {}) => (
    <div>
      <label htmlFor={`field-${name}`} className="mb-1.5 block text-sm font-medium text-[#2C2824]">
        {label}
      </label>
      <input
        id={`field-${name}`}
        className="studio-input"
        value={String((form as Record<string, unknown>)[name] ?? "")}
        onChange={(e) => {
          setForm({ ...form, [name]: e.target.value });
          if (["max_pages", "max_media", "max_social_files", "max_selected_images", "max_selected_videos"].includes(name)) {
            setActivePreset("custom");
          }
        }}
        {...props}
      />
      {helper && !errors[name] && <p className="mt-1 text-xs text-[#877D73]">{helper}</p>}
      {errors[name] && <p className="mt-1 text-xs font-medium text-rose-600">{errors[name]}</p>}
    </div>
  );

  return (
    <div className="space-y-6">
      <div>
        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-[#9A6B37]">
          Step 1 of 5 · Client Intake
        </div>
        <h1 className="mt-1 font-serif text-3xl font-medium tracking-tight text-[#181614]">
          New Media Collection
        </h1>
        <p className="mt-1 max-w-2xl text-sm text-[#6B635B]">
          Enter the salon&apos;s website and Facebook Page links below. We&apos;ll automatically gather available media from
          the past 365 days, verify quality, and prepare it for team review.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-12">
        {/* Main Intake Form */}
        <form onSubmit={onSubmit} className="studio-card space-y-6 p-6 sm:p-7 lg:col-span-8">
          {/* Section 1: Business & Links */}
          <div className="space-y-4">
            <div className="flex items-center gap-2.5 border-b border-[#F0E9E0] pb-3">
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[#181614] text-xs font-semibold text-white">
                1
              </span>
              <h2 className="text-base font-semibold text-[#181614]">Client Business &amp; Links</h2>
            </div>

            {field("business_name", "Salon / Business name", "Used to label the project and private Google Drive folder.", {
              placeholder: "e.g. Lily's Club Dog Grooming",
            })}
            {field("website_url", "Business website URL *", "Public salon website to scan for photos and videos.", {
              required: true,
              placeholder: "https://www.example-salon.com",
            })}
            {field("facebook_url", "Facebook Page URL *", "Public business Facebook Page for preceding 365-day posts.", {
              required: true,
              placeholder: "https://www.facebook.com/your-salon-page",
            })}

            <details className="rounded-xl border border-[#EDE6DC] bg-[#FAF8F5] px-4 py-3 text-sm">
              <summary className="cursor-pointer font-medium text-[#5C534B]">
                Add Instagram URL <span className="ml-1.5 rounded-full bg-white px-2 py-0.5 text-[11px] text-[#877D73] ring-1 ring-[#E4DCD1]">Optional · Deferred in V2.1</span>
              </summary>
              <div className="mt-3">
                {field("instagram_url", "Instagram profile URL (optional)", "Instagram anonymous downloads are parked in V2.1; leave blank for standard runs.", {
                  placeholder: "https://www.instagram.com/...",
                })}
              </div>
            </details>
          </div>

          {/* Section 2: Collection Size Preset */}
          <div className="space-y-4">
            <div className="flex items-center gap-2.5 border-b border-[#F0E9E0] pb-3">
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[#181614] text-xs font-semibold text-white">
                2
              </span>
              <div>
                <h2 className="text-base font-semibold text-[#181614]">Collection Scope</h2>
                <p className="text-xs text-[#7A7168]">Pick a ready-made preset — no technical tuning needed.</p>
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              {SIZE_PRESETS.map((preset) => {
                const selected = activePreset === preset.id;
                return (
                  <button
                    key={preset.id}
                    type="button"
                    onClick={() => applyPreset(preset)}
                    className={`rounded-xl border p-3.5 text-left transition ${
                      selected
                        ? "border-[#0F766E] bg-teal-50/50 ring-2 ring-[#0F766E]/20"
                        : "border-[#E5DDD2] bg-white hover:border-[#CBBDA9] hover:bg-[#FCFBF9]"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-1">
                      <span className="text-sm font-semibold text-[#181614]">{preset.title}</span>
                      <span
                        className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                          selected ? "bg-[#0F766E] text-white" : "bg-[#F4EFE8] text-[#6B635B]"
                        }`}
                      >
                        {preset.tag}
                      </span>
                    </div>
                    <p className="mt-1.5 text-xs leading-relaxed text-[#6B635B]">{preset.description}</p>
                  </button>
                );
              })}
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              {field("max_selected_images", "Max AI-selected photos", "AI will pick up to this many standout images.")}
              {field("max_selected_videos", "Max AI-selected videos", "AI will pick up to this many best video clips.")}
            </div>

            <details className="rounded-xl border border-[#EDE6DC] bg-[#FAF8F5] px-4 py-3 text-sm">
              <summary className="cursor-pointer font-medium text-[#5C534B]">
                Fine-tune scan limits (advanced)
              </summary>
              <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
                {field("max_pages", "Max website pages (1–25)")}
                {field("max_media", "Max website media (1–80)")}
                {field("max_social_files", "Max Facebook items (1–60)")}
              </div>
            </details>
          </div>

          {/* Section 3: Creative Direction & Permission */}
          <div className="space-y-4">
            <div className="flex items-center gap-2.5 border-b border-[#F0E9E0] pb-3">
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[#181614] text-xs font-semibold text-white">
                3
              </span>
              <h2 className="text-base font-semibold text-[#181614]">Style Direction &amp; Client Permission</h2>
            </div>

            <div>
              <label htmlFor="visual-prefs" className="mb-1.5 block text-sm font-medium text-[#2C2824]">
                Visual-direction preferences (optional)
              </label>
              <div className="mb-2 flex flex-wrap gap-1.5">
                {STYLE_CHIPS.map((chip) => (
                  <button
                    key={chip}
                    type="button"
                    onClick={() => appendStyleChip(chip)}
                    className="rounded-full border border-[#E2D9CE] bg-[#FAF8F5] px-2.5 py-1 text-xs font-medium text-[#544B43] transition hover:border-[#0F766E] hover:bg-teal-50/50 hover:text-[#0F766E]"
                  >
                    + {chip}
                  </button>
                ))}
              </div>
              <textarea
                id="visual-prefs"
                className="studio-input"
                rows={3}
                placeholder="Describe the look & feel you want the AI Creative Director to prioritize…"
                value={form.visual_preferences}
                onChange={(e) => setForm({ ...form, visual_preferences: e.target.value })}
              />
            </div>

            <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-amber-200 bg-amber-50/80 p-4 text-sm text-amber-950">
              <input
                type="checkbox"
                className="mt-1 h-4 w-4 rounded accent-[#0F766E]"
                checked={form.social_permission_confirmed}
                onChange={(e) => setForm({ ...form, social_permission_confirmed: e.target.checked })}
              />
              <span className="leading-relaxed">
                <strong className="font-semibold">Client Facebook media permission:</strong> I confirm client
                authorization to download permitted public Facebook media for this business (leave unchecked for
                metadata-only Facebook discovery).
              </span>
            </label>
          </div>

          {serverError && (
            <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
              {serverError}
            </div>
          )}

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[#F0E9E0] pt-4">
            <span className="text-xs text-[#7D746B]">
              No paid AI call runs yet — you&apos;ll review collected assets first.
            </span>
            <button disabled={submitting} className="studio-btn-primary px-6 py-2.5">
              {submitting ? "Starting Collection…" : "Start Collection"}
              {!submitting && <ArrowRightIcon className="h-4 w-4" />}
            </button>
          </div>
        </form>

        {/* Right Helpful Non-Technical Guide Sidebar */}
        <aside className="space-y-4 lg:col-span-4">
          <div className="studio-card p-5">
            <h3 className="text-sm font-semibold text-[#181614]">How Your Collection Works</h3>
            <ol className="mt-4 space-y-3.5 text-xs">
              {[
                {
                  step: "1",
                  title: "Gather & Deduplicate",
                  desc: "Fetches accessible website & Facebook media from the last 365 days and extracts video preview frames.",
                },
                {
                  step: "2",
                  title: "Independent Admin Review",
                  desc: "A second team admin reviews what was collected and approves the exact assets before AI runs.",
                },
                {
                  step: "3",
                  title: "AI Visual Director V2",
                  desc: "Scores real pixels & video frames, picks Hero shots, and writes art direction notes.",
                },
                {
                  step: "4",
                  title: "Per-Asset Rights Check",
                  desc: "Only assets explicitly marked rights-verified can ever be released.",
                },
                {
                  step: "5",
                  title: "Private Google Drive Folder",
                  desc: "Uploads approved originals into Best Images, Best Videos, and Reports with restricted access.",
                },
              ].map((item) => (
                <li key={item.step} className="flex items-start gap-3">
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-teal-50 text-[11px] font-bold text-[#0F766E] ring-1 ring-teal-200">
                    {item.step}
                  </span>
                  <div>
                    <div className="font-semibold text-[#181614]">{item.title}</div>
                    <p className="mt-0.5 leading-relaxed text-[#6B635B]">{item.desc}</p>
                  </div>
                </li>
              ))}
            </ol>
          </div>

          <div className="studio-card-soft space-y-3 p-5 text-xs text-[#5C534B]">
            <div className="flex items-center gap-2 font-semibold text-[#181614]">
              <ShieldCheckIcon className="h-4 w-4 text-[#0F766E]" />
              Built-In Safeguards
            </div>
            <ul className="space-y-2">
              {[
                "Respects public visibility and never bypasses logins or private pages.",
                "Separates visual quality from commercial reuse rights.",
                "Creates private-only Google Drive folders (never public links).",
              ].map((line) => (
                <li key={line} className="flex items-start gap-2">
                  <CheckIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[#0F766E]" />
                  <span>{line}</span>
                </li>
              ))}
            </ul>
            <div className="flex items-center gap-4 border-t border-[#E6DDD2] pt-3 text-[11px] text-[#7D746B]">
              <span className="inline-flex items-center gap-1">
                <SparklesIcon className="h-3.5 w-3.5 text-[#C8955A]" /> Real pixel AI
              </span>
              <span className="inline-flex items-center gap-1">
                <FolderCloudIcon className="h-3.5 w-3.5 text-[#0F766E]" /> Drive v3 ready
              </span>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
