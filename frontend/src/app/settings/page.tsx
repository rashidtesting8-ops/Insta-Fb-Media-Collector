"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type HealthResponse = {
  worker?: { ok: boolean; status?: number; collection_engine?: string; m4_approval_enabled?: boolean };
  n8n?: { ok: boolean; status?: number };
  ai?: { configured: boolean; provider?: string | null; model?: string | null };
  drive?: { configured: boolean; delivery_webhook_configured?: boolean };
};

export default function Settings() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newRole, setNewRole] = useState<"admin" | "reviewer">("admin");
  const [adminStatus, setAdminStatus] = useState<{ ok: boolean; text: string } | null>(null);
  const [creating, setCreating] = useState(false);

  async function refreshHealth() {
    setError(null);
    try {
      const data = await api<HealthResponse>("/api/health");
      setHealth(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
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
        body: JSON.stringify({ name: newName.trim(), password: newPassword, role: newRole }),
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
        text: `Created ${newRole} account (${data.id || "recorded"}).`,
      });
    } catch {
      setAdminStatus({ ok: false, text: "Named Admin service unavailable." });
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Settings / System Status</h1>
        <button
          type="button"
          onClick={refreshHealth}
          className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50"
        >
          Refresh status
        </button>
      </div>
      {error && <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-medium">Connectivity & Engine</h2>
        <ul className="space-y-2 text-sm">
          <li>
            Worker:{" "}
            <span className={health?.worker?.ok ? "font-medium text-green-600" : "font-medium text-red-600"}>
              {health?.worker?.ok ? "OK" : "Unavailable"}
            </span>
          </li>
          <li>
            Collection engine:{" "}
            <span className="font-mono text-slate-700">{health?.worker?.collection_engine ?? "unknown"}</span>
          </li>
          <li>
            M4 Approval & Rights Gate:{" "}
            <span className={health?.worker?.m4_approval_enabled ? "text-green-600" : "text-amber-600"}>
              {health?.worker?.m4_approval_enabled ? "Enabled" : "Disabled"}
            </span>
          </li>
          <li>
            n8n:{" "}
            <span className={health?.n8n?.ok ? "font-medium text-green-600" : "font-medium text-red-600"}>
              {health?.n8n?.ok ? "OK" : "Unavailable"}
            </span>
          </li>
        </ul>
      </div>
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-medium">Integrations</h2>
        <ul className="space-y-2 text-sm">
          <li>
            AI provider configured (worker):{" "}
            <span className={health?.ai?.configured ? "font-medium text-green-600" : "font-medium text-amber-600"}>
              {health?.ai?.configured
                ? `Yes (${health.ai.provider ?? "configured"}${health.ai.model ? ` · ${health.ai.model}` : ""})`
                : "No (AI curation blocked)"}
            </span>
          </li>
          <li>
            Google Drive ready:{" "}
            <span className={health?.drive?.configured ? "font-medium text-green-600" : "font-medium text-amber-600"}>
              {health?.drive?.configured ? "Yes" : "No (set GOOGLE_DRIVE_READY=1 once OAuth credential is connected)"}
            </span>
          </li>
          <li>
            Drive delivery webhook token:{" "}
            <span className={health?.drive?.delivery_webhook_configured ? "text-green-600" : "text-amber-600"}>
              {health?.drive?.delivery_webhook_configured ? "Configured" : "Not configured (N8N_ADMIN_WEBHOOK_TOKEN)"}
            </span>
          </li>
        </ul>
        <p className="mt-3 text-xs text-slate-500">
          Readiness is queried live from the worker container. No secrets are displayed.
        </p>
      </div>
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-2 font-medium">Named Admin Accounts (Two-Admin Approval)</h2>
        <p className="mb-4 text-xs text-slate-600">
          M4 collection approval requires an independent Named Admin separate from the job submitter. Sign in as an
          existing Named Admin to provision a second Admin or Reviewer account.
        </p>
        <form onSubmit={onCreateAdmin} className="max-w-md space-y-3 text-sm">
          <label className="block">
            Account name
            <input
              type="text"
              required
              minLength={2}
              maxLength={64}
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="reviewer-admin"
              className="mt-1 block w-full rounded border border-slate-300 px-3 py-1.5"
            />
          </label>
          <label className="block">
            Password (min 12 chars)
            <input
              type="password"
              required
              minLength={12}
              autoComplete="new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className="mt-1 block w-full rounded border border-slate-300 px-3 py-1.5"
            />
          </label>
          <label className="block">
            Role
            <select
              value={newRole}
              onChange={(e) => setNewRole(e.target.value === "reviewer" ? "reviewer" : "admin")}
              className="mt-1 block w-full rounded border border-slate-300 px-3 py-1.5"
            >
              <option value="admin">Admin (can approve, start AI, verify rights, create accounts)</option>
              <option value="reviewer">Reviewer (read-only named session)</option>
            </select>
          </label>
          {adminStatus && (
            <p className={adminStatus.ok ? "text-green-700" : "text-red-600"} role="status">
              {adminStatus.text}
            </p>
          )}
          <button
            type="submit"
            disabled={creating}
            className="rounded bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {creating ? "Creating…" : "Create Named Account"}
          </button>
        </form>
      </div>
    </div>
  );
}
