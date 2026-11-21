/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export default function Settings() {
  const [health, setHealth] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api("/api/health").then(setHealth).catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
    // eslint-disable-next-line react-hooks/set-state-in-effect
  }, []);
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Settings / System Status</h1>
      {error && <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-medium">Connectivity</h2>
        <ul className="space-y-2 text-sm">
          <li>Worker: <span className={(health as any)?.worker?.ok ? "text-green-600" : "text-red-600"}>{(health as any)?.worker?.ok ? "OK" : "Unavailable"}</span></li>
          <li>n8n: <span className={(health as any)?.n8n?.ok ? "text-green-600" : "text-red-600"}>{(health as any)?.n8n?.ok ? "OK" : "Unavailable"}</span></li>
        </ul>
      </div>
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-medium">Integrations</h2>
        <ul className="space-y-2 text-sm">
          <li>AI provider configured: <span className={(health as any)?.ai?.configured ? "text-green-600" : "text-amber-600"}>{(health as any)?.ai?.configured ? "Yes" : "No (AI curation blocked)"}</span></li>
          <li>Google Drive ready: <span className={(health as any)?.drive?.configured ? "text-green-600" : "text-amber-600"}>{(health as any)?.drive?.configured ? "Yes" : "No (delivery blocked)"}</span></li>
        </ul>
        <p className="mt-3 text-xs text-slate-500">No secrets are displayed. Configure via server environment variables.</p>
      </div>
    </div>
  );
}
