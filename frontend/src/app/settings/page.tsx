"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { FolderCloudIcon, RefreshIcon, ShieldCheckIcon, SparklesIcon } from "@/components/Icons";

type HealthResponse = {
  worker?: {
    ok: boolean;
    status?: number;
    collection_engine?: string;
    m4_approval_enabled?: boolean;
    playwright_fallback_enabled?: boolean;
    playwright_installed?: boolean;
  };
  n8n?: { ok: boolean; status?: number };
  ai?: {
    configured: boolean;
    provider?: string | null;
    model?: string | null;
    tier_preset?: string | null;
    max_images_per_request?: number;
    mode_b_max_output_tokens?: number;
  };
  drive?: { configured: boolean; delivery_webhook_configured?: boolean };
};

export default function Settings() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [adminStatus, setAdminStatus] = useState<{ ok: boolean; text: string } | null>(null);
  const [creating, setCreating] = useState(false);

  async function refreshHealth() {
    setRefreshing(true);
    setError(null);
    try {
      const data = await api<HealthResponse>("/api/health");
      setHealth(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
  }

  useEffect(() => {
    let active = true;
    api<HealthResponse>("/api/health")
      .then((data) => {
        if (active) {
          setError(null);
          setHealth(data);
        }
      })
      .catch((e: unknown) => {
        if (active) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      active = false;
    };
  }, []);

  async function onCreateAdmin(e: React.FormEvent) {
    e.preventDefault();
    if (creating) return;
    setCreating(true);
    setAdminStatus(null);
    try {
      const res = await fetch("/api/auth/named/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName.trim(), password: newPassword }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setAdminStatus({
          ok: false,
          text:
            res.status === 401
              ? "Sign in as an existing Named Admin to create additional accounts."
              : typeof data.error === "string"
                ? data.error
                : "Account creation rejected.",
        });
        return;
      }
      setNewName("");
      setNewPassword("");
      setAdminStatus({
        ok: true,
        text: `Created Named Admin account (${data.principal_id || "recorded"}).`,
      });
    } catch {
      setAdminStatus({ ok: false, text: "Named Admin service unavailable." });
    } finally {
      setCreating(false);
    }
  }

  const statusCards = [
    {
      title: "Media Collection Engine",
      ok: Boolean(health?.worker?.ok),
      statusText: health?.worker?.ok ? "Online & Ready" : "Unavailable",
      detail: `Engine: ${health?.worker?.collection_engine ?? "checking…"} · Approval Gate: ${
        health?.worker?.m4_approval_enabled ? "Enabled" : "Disabled"
      } · JS Gallery Fallback: ${
        health?.worker?.playwright_fallback_enabled && health?.worker?.playwright_installed
          ? "Active (Chromium)"
          : health?.worker?.playwright_fallback_enabled
            ? "Enabled (Chromium not installed)"
            : "Static HTML only"
      }`,
      Icon: ShieldCheckIcon,
    },
    {
      title: "Workflow Automation (n8n)",
      ok: Boolean(health?.n8n?.ok),
      statusText: health?.n8n?.ok ? "Connected" : "Unavailable",
      detail: "Orchestrates intake validation, job status tracking, and Google Drive uploads.",
      Icon: RefreshIcon,
    },
    {
      title: "AI Visual Director V2",
      ok: Boolean(health?.ai?.configured),
      statusText: health?.ai?.configured ? "Configured" : "Not Configured",
      detail: health?.ai?.configured
        ? `${health.ai.provider ?? "Provider"} · ${health.ai.model ?? "Vision Model"}${
            health.ai.tier_preset
              ? ` · Tier: ${health.ai.tier_preset}${
                  health.ai.max_images_per_request
                    ? ` (${health.ai.max_images_per_request} img/req, ${health.ai.mode_b_max_output_tokens ?? "—"} out tok)`
                    : ""
                }`
              : ""
          }`
        : "AI curation requires a configured vision provider in worker environment.",
      Icon: SparklesIcon,
    },
    {
      title: "Private Google Drive Delivery",
      ok: Boolean(health?.drive?.configured && health?.drive?.delivery_webhook_configured),
      statusText:
        health?.drive?.configured && health?.drive?.delivery_webhook_configured ? "Ready for Uploads" : "Needs Setup",
      detail: `OAuth Flag: ${health?.drive?.configured ? "Ready" : "Off"} · Delivery Webhook: ${
        health?.drive?.delivery_webhook_configured ? "Configured" : "Missing"
      }`,
      Icon: FolderCloudIcon,
    },
  ];

  return (
    <div className="space-y-7">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="text-xs font-semibold uppercase tracking-[0.14em] text-[#9A6B37]">
            Studio Administration
          </div>
          <h1 className="mt-1 font-serif text-3xl font-medium tracking-tight text-[#181614]">
            Settings &amp; System Status
          </h1>
          <p className="mt-1 text-sm text-[#6B635B]">
            Verify live service health and manage Named Admin accounts for two-person review safety.
          </p>
        </div>
        <button type="button" onClick={refreshHealth} disabled={refreshing} className="studio-btn-secondary">
          <RefreshIcon className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
          {refreshing ? "Checking…" : "Refresh status"}
        </button>
      </div>

      {error && (
        <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          {error}
        </div>
      )}

      {/* System Status Cards */}
      <div className="grid gap-4 sm:grid-cols-2">
        {statusCards.map(({ title, ok, statusText, detail, Icon }) => (
          <div key={title} className="studio-card flex items-start gap-3.5 p-5">
            <div
              className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${
                ok ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-800"
              }`}
            >
              <Icon className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-sm font-semibold text-[#181614]">{title}</h2>
                <span
                  className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                    ok
                      ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                      : "border-amber-200 bg-amber-50 text-amber-900"
                  }`}
                >
                  <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-emerald-500" : "bg-amber-500"}`} />
                  {statusText}
                </span>
              </div>
              <p className="mt-1.5 text-xs leading-relaxed text-[#6B635B]">{detail}</p>
            </div>
          </div>
        ))}
      </div>
      <p className="text-xs text-[#877D73]">
        Live readiness is queried directly from the local stack. API keys, OAuth tokens, and passwords are never displayed.
      </p>

      {/* Named Admin Account Provisioning */}
      <section className="studio-card p-6">
        <div className="flex items-start gap-3 border-b border-[#F0E9E0] pb-4">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-teal-50 text-[#0F766E]">
            <ShieldCheckIcon className="h-5 w-5" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-[#181614]">Named Admin Accounts (Two-Admin Approval)</h2>
            <p className="mt-0.5 max-w-2xl text-xs leading-relaxed text-[#6B635B]">
              To protect client media, the admin who submits a collection cannot approve their own AI curation run. Add
              a second Named Admin here so another team member can independently approve jobs and verify rights.
            </p>
          </div>
        </div>

        <form onSubmit={onCreateAdmin} className="mt-5 max-w-md space-y-4 text-sm">
          <div>
            <label htmlFor="new-admin-name" className="mb-1.5 block font-medium text-[#2C2824]">
              Account name
            </label>
            <input
              id="new-admin-name"
              type="text"
              required
              minLength={2}
              maxLength={64}
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="e.g. approver-admin"
              className="studio-input"
            />
          </div>

          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <label htmlFor="new-admin-pass" className="block font-medium text-[#2C2824]">
                Password (min 12 characters)
              </label>
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                className="text-xs font-medium text-[#0F766E] hover:underline"
              >
                {showPassword ? "Hide" : "Show"}
              </button>
            </div>
            <input
              id="new-admin-pass"
              type={showPassword ? "text" : "password"}
              required
              minLength={12}
              autoComplete="new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="At least 12 characters"
              className="studio-input"
            />
          </div>

          {adminStatus && (
            <div
              role="status"
              className={`rounded-xl border px-3.5 py-2.5 text-xs font-medium ${
                adminStatus.ok
                  ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                  : "border-rose-200 bg-rose-50 text-rose-800"
              }`}
            >
              {adminStatus.text}
            </div>
          )}

          <button type="submit" disabled={creating} className="studio-btn-primary">
            {creating ? "Creating account…" : "Create Named Admin"}
          </button>
        </form>
      </section>
    </div>
  );
}
